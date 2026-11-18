"""数据血缘追踪 DAG 图服务：生成节点、边示例结构与流转规则。

跨模块职责（去重服务 → 血缘服务）：
- 血缘服务把去重服务算出的「保留记录台账及重复项引用」持久化在本服务仓库里；
- 血缘服务只把台账作为只读输入做核对，不改动台账本身，核对结果单独回传给去重服务；
- 每个保留项绑定到下游字段关系，支持血缘页按保留项回查被合并的数据快照。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "lineage"
RETENTION_LEDGER_MODULE = "lineage_retention_ledger"
RETENTION_FIX_MODULE = "lineage_retention_fix"
RETENTION_BIND_MODULE = "lineage_retention_binding"
RETENTION_CHECK_MODULE = "lineage_retention_check"
REQUIRED_FIELDS = ["关系编号", "上游节点", "下游节点"]
STATUS_ORDER = ["草稿", "已记录", "已验证", "已归档"]
ACTION_RULES = {"记录关系": "已记录", "验证关系": "已验证", "归档关系": "已归档"}
NEGATIVE_ACTIONS = []


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


class RetentionLoadConflict(Exception):
    """同一策略已有装载在进行中时，后续并发请求直接拒绝。"""


class LineageService:
    def get_graph(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        nodes: list[dict] = []
        edges: list[dict] = []
        for i, row in enumerate(rows):
            nodes.append({"id": int(row.get("id", i+1)), "name": str(row.get(list(row.keys())[1] if len(row)>1 else "name", "")), "status": str(row.get("status", STATUS_ORDER[0])),
                          **{k: row[k] for k in row if k not in ("id","status","pending","abnormal")}})
        for i in range(len(nodes)-1):
            edges.append({"from": nodes[i]["id"], "to": nodes[i+1]["id"], "label": "依赖"})
        return {"nodes": nodes, "edges": edges}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id",0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"血缘关系 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据血缘追踪可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"血缘关系已{action}"

    def raw_data(self) -> tuple[list[dict], list[dict]]:
        g = self.get_graph()
        return g["nodes"], g["edges"]

    # ------------------------------------------------------------------
    # 保留记录台账：持久化、只读核对、下游字段绑定与回查
    # ------------------------------------------------------------------
    def _next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1

    def latest_version(self, strategy_id: int) -> int:
        """取某策略已持久化台账的最新版本号；没有则返回 0。"""
        versions = [
            int(row.get("版本", 0))
            for row in store.rows(RETENTION_LEDGER_MODULE)
            if int(row.get("策略id", 0)) == int(strategy_id)
        ]
        return max(versions, default=0)

    def has_active_load(self, strategy_id: int) -> bool:
        return strategy_id in self._active_loads

    def persist_retention(
        self,
        *,
        strategy: dict[str, Any],
        ledger: list[dict[str, Any]],
        fix_items: list[dict[str, Any]],
        batch_no: str,
        version: int,
    ) -> dict[str, Any]:
        """把去重服务的计算结果在血缘服务落库；台账内容对血缘只读，原样持久化。"""
        ledger_rows = store.rows(RETENTION_LEDGER_MODULE)
        fix_rows = store.rows(RETENTION_FIX_MODULE)
        for item in ledger:
            row = {"id": self._next_id(RETENTION_LEDGER_MODULE), "status": "已记录",
                   "pending": item.get("是否冲突", False), "abnormal": item.get("是否冲突", False)}
            row.update(item)
            ledger_rows.append(row)
        for item in fix_items:
            row = {"id": self._next_id(RETENTION_FIX_MODULE), "status": "待修复",
                   "pending": True, "abnormal": True}
            row.update(item)
            fix_rows.append(row)

        # 持久化完成后立即做只读核对，并把核对结果单独保存、回传给去重服务。
        check = self.verify_ledger(strategy_id=int(strategy["id"]), version=version)
        check_rows = store.rows(RETENTION_CHECK_MODULE)
        check_rows.append({"id": self._next_id(RETENTION_CHECK_MODULE), **check})

        # 把每个保留项绑定到下游字段关系（血缘图节点），形成保留项 → 下游字段链路。
        self._bind_ledger_to_lineage(strategy=strategy, ledger=ledger, batch_no=batch_no, version=version)
        return check

    def _bind_ledger_to_lineage(
        self,
        *,
        strategy: dict[str, Any],
        ledger: list[dict[str, Any]],
        batch_no: str,
        version: int,
    ) -> None:
        """把保留项绑定到血缘 DAG 的下游节点；没有现成节点时按策略生成确定性的下游字段节点。"""
        lineage_rows = store.rows(MODULE)
        binding_rows = store.rows(RETENTION_BIND_MODULE)
        target_nodes = lineage_rows or []
        for index, item in enumerate(ledger):
            target = target_nodes[index % len(target_nodes)] if target_nodes else {
                "id": index + 1, "关系编号": f"LINE-AUTO-{index + 1:03d}",
                "下游节点": f"{strategy.get('去重键')}下游字段", "目标字段": strategy.get("去重键"),
            }
            binding_rows.append({
                "id": self._next_id(RETENTION_BIND_MODULE),
                "status": "已绑定",
                "pending": False,
                "abnormal": False,
                "绑定编号": f"BIND-{batch_no}-{len(binding_rows) + 1:03d}",
                "批次号": batch_no,
                "版本": version,
                "策略编号": item.get("策略编号"),
                "台账编号": item.get("台账编号"),
                "主保留记录编号": item.get("主保留记录编号"),
                "去重键值": item.get("去重键值"),
                "下游血缘id": target.get("id"),
                "下游节点": target.get("下游节点") or target.get("关系编号"),
                "目标字段": target.get("目标字段") or strategy.get("去重键"),
                "绑定时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

    def verify_ledger(self, *, strategy_id: int, version: int | None = None) -> dict[str, Any]:
        """只读核对：固定去重键/窗口/排序依据，且同一窗口不得出现两个主保留项。"""
        rows = [
            row for row in store.rows(RETENTION_LEDGER_MODULE)
            if int(row.get("策略id", 0)) == int(strategy_id)
            and (version is None or int(row.get("版本", 0)) == int(version))
        ]
        problems: list[str] = []
        # 唯一主记录约束按版本生效：同一版本内 (去重键值, 窗口) 只能有一个主保留项；
        # 不同版本是重算保留下来的历史台账，窗口键相同属于预期，不能误判。
        seen: dict[tuple[Any, str, str], str] = {}
        for row in rows:
            key = (row.get("版本"), str(row.get("去重键值")), str(row.get("窗口依据")))
            if key in seen:
                problems.append(
                    f"V{key[0]} 去重键值 {key[1]} 在窗口 {key[2]} 出现两个主保留项："
                    f"{seen[key]} 与 {row.get('主保留记录编号')}"
                )
            else:
                seen[key] = str(row.get("主保留记录编号"))
            if not row.get("处理依据"):
                problems.append(f"台账 {row.get('台账编号')} 缺少固定处理依据")
            for ref in row.get("重复项引用", []) or []:
                if ref.get("数据快照") is None:
                    problems.append(
                        f"台账 {row.get('台账编号')} 的重复项 {ref.get('来源记录编号')} 缺少数据快照，无法回查"
                    )
        ok = not problems
        return {
            "核对时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "策略id": strategy_id,
            "版本": version,
            "台账条目数": len(rows),
            "冲突条目数": sum(1 for row in rows if row.get("是否冲突")),
            "核对结果": "通过" if ok else "不通过",
            "问题明细": problems,
            "ok": ok,
        }

    def list_retention_ledger(
        self, *, strategy_id: int | None = None, version: int | None = None,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(RETENTION_LEDGER_MODULE)
        if strategy_id is not None:
            rows = [row for row in rows if int(row.get("策略id", 0)) == int(strategy_id)]
        if version is not None:
            rows = [row for row in rows if int(row.get("版本", 0)) == int(version)]
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("去重键值", "")).casefold()
                or needle in str(row.get("主保留记录编号", "")).casefold()
                or needle in str(row.get("台账编号", "")).casefold()
            ]
        return rows

    def list_fix_items(self, *, strategy_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RETENTION_FIX_MODULE)
        if strategy_id is not None:
            rows = [row for row in rows if self._strategy_id_of(row) == int(strategy_id)]
        if status:
            rows = [row for row in rows if row.get("处理状态") == status]
        return rows

    def _strategy_id_of(self, fix_row: dict[str, Any]) -> int:
        for row in store.rows(RETENTION_LEDGER_MODULE):
            if row.get("策略编号") == fix_row.get("策略编号") and row.get("批次号") == fix_row.get("批次号"):
                return int(row.get("策略id", 0))
        return -1

    def list_bindings(self, *, keyword: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RETENTION_BIND_MODULE)
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("主保留记录编号", "")).casefold()
                or needle in str(row.get("去重键值", "")).casefold()
                or needle in str(row.get("台账编号", "")).casefold()
            ]
        return rows

    def lookup_by_retention(self, retention_no: str) -> dict[str, Any]:
        """血缘页按主保留记录编号回查：返回保留项、重复项快照与下游字段绑定。"""
        ledger_rows = [
            row for row in store.rows(RETENTION_LEDGER_MODULE)
            if str(row.get("主保留记录编号")) == retention_no
            or str(row.get("台账编号")) == retention_no
        ]
        if not ledger_rows:
            return {"found": False, "message": f"未找到保留项 {retention_no} 的血缘回查记录"}
        ledger = ledger_rows[-1]
        bindings = [
            row for row in store.rows(RETENTION_BIND_MODULE)
            if row.get("台账编号") == ledger.get("台账编号")
        ]
        return {"found": True, "retention": ledger, "bindings": bindings}

    def retention_summary(self) -> dict[str, Any]:
        ledger = store.rows(RETENTION_LEDGER_MODULE)
        fixes = store.rows(RETENTION_FIX_MODULE)
        bindings = store.rows(RETENTION_BIND_MODULE)
        return {
            "台账条目数": len(ledger),
            "主保留记录数": len(ledger),
            "重复项引用总数": sum(int(row.get("重复项数量", 0)) for row in ledger),
            "待修复条目数": sum(1 for row in fixes if row.get("处理状态") == "待修复"),
            "下游绑定数": len(bindings),
            "策略数": len({row.get("策略编号") for row in ledger}),
        }

    # 并发装载：同一策略只允许一个装载请求完成，其余在入口处直接拒绝。
    # check-and-set 必须在锁内完成，避免两个并发请求同时通过存在性判断。
    _active_loads: set[int] = set()
    _load_guard = threading.Lock()

    def try_begin_load(self, strategy_id: int) -> None:
        with self._load_guard:
            if strategy_id in self._active_loads:
                raise RetentionLoadConflict(
                    f"策略 {strategy_id} 的保留台账正在装载中，仅首个请求生效，本请求拒绝"
                )
            self._active_loads.add(strategy_id)

    def end_load(self, strategy_id: int) -> None:
        with self._load_guard:
            self._active_loads.discard(strategy_id)
