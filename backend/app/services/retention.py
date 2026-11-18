"""保留记录台账核心计算：按去重键 + 时间窗口计算唯一主保留项与重复项引用。

本模块只做纯计算，不直接读写仓库；去重服务负责取数和触发，血缘服务负责持久化。
两个边界口径都在这里实现，保证「一个去重窗口最终只能有一个主保留记录」：

- 排序字段优先：窗口内按排序值（优先级数字小者优先）选主，较晚记录只作补充证据；
- 时间窗口末位优先：窗口内按记录时间末位选主，前置优先级差异进入引用说明。

当两种口径选出的主记录不一致时，即为窗口边界冲突，需要进待修复清单，
最终以策略声明的保留策略（冲突口径）重算定主，旧依据保留为历史版本。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# 保留策略口径常量，避免在各服务里散落魔法字符串。
POLICY_SORT_FIRST = "排序字段优先"
POLICY_WINDOW_LAST = "时间窗口末位优先"
SUPPORTED_POLICIES = (POLICY_SORT_FIRST, POLICY_WINDOW_LAST)

WINDOW_UNIT_SECONDS = {
    "小时": 3600,
    "小时钟": 3600,
    "天": 86400,
    "日": 86400,
}


def parse_window(window_text: Any) -> tuple[int, str]:
    """把「1天 / 2小时」解析成 (秒数, 单位)；无法识别时按 1 天兜底。"""
    text = str(window_text or "").strip()
    digits = ""
    unit = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif ch.isalpha() or ch.isspace():
            unit += ch.strip()
    amount = int(digits) if digits else 1
    unit = unit or "天"
    seconds = amount * WINDOW_UNIT_SECONDS.get(unit, 86400)
    return seconds, unit or "天"


def parse_time(value: Any) -> datetime:
    """解析记录时间，支持 'YYYY-MM-DD HH:MM' 与 'YYYY-MM-DD'。"""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 实在解析不出来时放到最早，保证排序稳定而不是抛异常中断整批装载。
    return datetime.min


def priority_of(record: dict[str, Any]) -> int:
    """排序值取数字优先级；缺失时给一个较大的兜底值，避免 None 比较报错。"""
    raw = record.get("优先级")
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw or record.get("排序值") or "").strip().upper()
    if text.startswith("P") and text[1:].isdigit():
        return int(text[1:])
    return 9999


def window_bounds(moment: datetime, window_seconds: int) -> tuple[datetime, datetime]:
    """以固定窗口宽度从固定纪元对齐切窗，保证同一记录每次落到同一个窗口。"""
    if window_seconds <= 0:
        window_seconds = 86400
    epoch = datetime(2000, 1, 1)
    offset = int((moment - epoch).total_seconds())
    index = offset // window_seconds
    start = epoch + timedelta(seconds=index * window_seconds)
    return start, start + timedelta(seconds=window_seconds)


def _sort_first_winner(records: list[dict[str, Any]]) -> dict[str, Any]:
    """排序字段优先：优先级小者为主，同优先级取较早记录，保证结果确定。"""
    return min(records, key=lambda r: (priority_of(r), parse_time(r["记录时间"]), int(r.get("id", 0))))


def _window_last_winner(records: list[dict[str, Any]]) -> dict[str, Any]:
    """时间窗口末位优先：记录时间晚者为主，同时间取 id 大者，保证结果确定。"""
    return max(records, key=lambda r: (parse_time(r["记录时间"]), int(r.get("id", 0))))


def _build_reference(record: dict[str, Any], reason: str) -> dict[str, Any]:
    """重复项引用：被合并记录必须带回数据快照，支持下游回查比较依据。"""
    return {
        "来源记录编号": record.get("来源记录编号"),
        "来源记录id": record.get("id"),
        "记录时间": record.get("记录时间"),
        "排序值": record.get("排序值"),
        "优先级": record.get("优先级"),
        "完整度": record.get("完整度"),
        "合并原因": reason,
        "数据快照": record.get("数据快照"),
    }


def build_ledger(
    strategy: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    version: int,
    batch_no: str,
    generated_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按策略对一批来源记录计算保留台账与待修复清单。

    返回 (ledger_entries, fix_items)：
    - 每个 (去重键值, 窗口) 桶只产出一条主保留记录，满足唯一主记录约束；
    - 桶内其余记录全部进入重复项引用并保留快照；
    - 双口径选主不一致时记一条冲突待修复项。
    """
    window_seconds, window_unit = parse_window(strategy.get("时间窗口"))
    policy = str(strategy.get("保留策略") or POLICY_SORT_FIRST).strip()
    if policy not in SUPPORTED_POLICIES:
        policy = POLICY_SORT_FIRST
    sort_field = str(strategy.get("排序字段") or "优先级")
    dedup_key = str(strategy.get("去重键") or "")

    buckets: dict[tuple[str, datetime, datetime], list[dict[str, Any]]] = {}
    for record in records:
        moment = parse_time(record.get("记录时间"))
        start, end = window_bounds(moment, window_seconds)
        buckets.setdefault((str(record.get("去重键值")), start, end), []).append(record)

    ledger: list[dict[str, Any]] = []
    fix_items: list[dict[str, Any]] = []
    for (key_value, start, end), bucket in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1])):
        sort_winner = _sort_first_winner(bucket)
        last_winner = _window_last_winner(bucket)
        conflict = sort_winner.get("id") != last_winner.get("id")

        # 按冲突口径（策略声明的保留策略）重算定主，两个口径都只会给出同一条主记录。
        winner = sort_winner if policy == POLICY_SORT_FIRST else last_winner
        references: list[dict[str, Any]] = []
        for record in sorted(bucket, key=lambda r: (parse_time(r["记录时间"]), int(r.get("id", 0)))):
            if record.get("id") == winner.get("id"):
                continue
            if policy == POLICY_SORT_FIRST:
                reason = (
                    f"排序字段「{sort_field}」优先级 {priority_of(winner)} 高于本记录 "
                    f"{priority_of(record)}，本记录作为补充证据并入主保留项"
                )
            else:
                reason = (
                    f"时间窗口末位优先，主记录时间 {winner.get('记录时间')} 晚于本记录 "
                    f"{record.get('记录时间')}，前置优先级差异进入引用说明"
                )
            references.append(_build_reference(record, reason))

        window_label = f"{start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')}"
        basis = (
            f"口径={policy}；去重键={dedup_key}；窗口={window_seconds // WINDOW_UNIT_SECONDS.get(window_unit, 86400)}"
            f"{window_unit}（{window_label}）；排序字段={sort_field}"
        )
        ledger.append({
            "台账编号": f"RT-{batch_no}-{len(ledger) + 1:03d}",
            "批次号": batch_no,
            "版本": version,
            "策略编号": strategy.get("策略编号"),
            "策略id": strategy.get("id"),
            "去重键": dedup_key,
            "去重键值": key_value,
            "窗口开始": start.strftime("%Y-%m-%d %H:%M"),
            "窗口结束": end.strftime("%Y-%m-%d %H:%M"),
            "窗口依据": window_label,
            "排序字段": sort_field,
            "保留策略": policy,
            "处理依据": basis,
            "主保留记录编号": winner.get("来源记录编号"),
            "主保留记录id": winner.get("id"),
            "主保留快照": winner.get("数据快照"),
            "重复项数量": len(references),
            "重复项引用": references,
            "是否冲突": conflict,
            "排序字段口径主记录": sort_winner.get("来源记录编号"),
            "窗口末位口径主记录": last_winner.get("来源记录编号"),
            "台账状态": "待修复" if conflict else "已核对",
            "生成时间": generated_at,
        })

        if conflict:
            fix_items.append({
                "清单编号": f"FIX-{batch_no}-{len(fix_items) + 1:03d}",
                "批次号": batch_no,
                "版本": version,
                "策略编号": strategy.get("策略编号"),
                "去重键值": key_value,
                "窗口依据": window_label,
                "冲突类型": "窗口边界双口径主记录不一致",
                "排序字段口径主记录": sort_winner.get("来源记录编号"),
                "窗口末位口径主记录": last_winner.get("来源记录编号"),
                "最终采用口径": policy,
                "最终主保留记录": winner.get("来源记录编号"),
                "修复说明": (
                    f"同一窗口内排序字段优先选 {sort_winner.get('来源记录编号')}、"
                    f"时间窗口末位优先选 {last_winner.get('来源记录编号')}；按冲突口径「{policy}」"
                    f"重算后以 {winner.get('来源记录编号')} 为唯一主保留记录，旧依据保留在历史版本"
                ),
                "处理状态": "待修复",
                "生成时间": generated_at,
            })
    return ledger, fix_items
