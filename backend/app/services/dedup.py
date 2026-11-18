"""采集去重业务规则：状态流转、字段校验、保留记录台账装载与重算。

调用链（去重服务 → 血缘服务）：
去重服务读取「采集去重策略 + 输入来源记录」，按去重键和时间窗口计算保留项，
再把保留记录台账及重复项引用交给血缘服务持久化；血缘服务以只读输入做核对，
把核对结果回传给去重服务。同一策略并发装载仅首个请求完成，其余直接拒绝。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.lineage import LineageService, RetentionLoadConflict
from app.services.retention import build_ledger
from app.store import store

MODULE = "dedup"
SOURCE_MODULE = "dedup_source_records"
LIST_FIELDS = ["策略编号", "输入来源", "去重键", "排序字段", "保留策略", "时间窗口", "重复阈值", "策略状态"]
REQUIRED_FIELDS = ["策略编号", "输入来源", "去重键"]
STATUS_ORDER = ["草稿", "已配置", "生效中", "已停用"]
ACTION_RULES = {"提交配置": "已配置", "启用策略": "生效中", "停用策略": "已停用"}
NEGATIVE_ACTIONS = ["停用策略"]


class RetentionRejected(Exception):
    """装载被拒绝（并发冲突或无数据），由路由层转成可读的失败结果。"""


class DedupService:
    def __init__(self, lineage_service: LineageService | None = None) -> None:
        # 跨模块连接：去重服务沿调用链定位到血缘服务并复用同一个持久化实例。
        self.lineage = lineage_service or LineageService()

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("策略编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去重策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集去重可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"去重策略已{action}"

    # ------------------------------------------------------------------
    # 输入来源记录
    # ------------------------------------------------------------------
    def list_source_records(self, strategy_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SOURCE_MODULE)
        if strategy_id is None:
            return rows
        code = self._strategy_code_by_id(strategy_id)
        if code is None:
            return []
        return [row for row in rows if str(row.get("策略编号")) == code]

    def _strategy_code_by_id(self, strategy_id: int) -> str | None:
        entry = store.find(MODULE, strategy_id)
        return str(entry.get("策略编号")) if entry else None

    def _records_for_strategy(self, strategy: dict[str, Any]) -> list[dict[str, Any]]:
        code = str(strategy.get("策略编号"))
        return [
            row for row in store.rows(SOURCE_MODULE)
            if str(row.get("策略编号")) == code
        ]

    # ------------------------------------------------------------------
    # 保留记录台账装载 / 重算
    # ------------------------------------------------------------------
    def _compute_and_persist(
        self, strategy: dict[str, Any], *, recompute: bool, reason: str
    ) -> dict[str, Any]:
        strategy_id = int(strategy["id"])
        # 并发装载：仅首个请求能进入，其余在血缘服务入口被拒绝。
        try:
            self.lineage.try_begin_load(strategy_id)
        except RetentionLoadConflict as exc:
            raise RetentionRejected(str(exc)) from exc

        try:
            records = self._records_for_strategy(strategy)
            if not records:
                raise RetentionRejected(
                    f"策略 {strategy.get('策略编号')} 没有可装载的输入来源记录，无法生成保留台账"
                )

            previous_version = self.lineage.latest_version(strategy_id)
            if not recompute and previous_version > 0:
                # 首次装载一旦落库，后续装载请求全部拒绝；更新口径只能走重算，保留旧版本。
                raise RetentionRejected(
                    f"策略 {strategy.get('策略编号')} 已装载过保留台账（最新 V{previous_version}），"
                    "并发装载仅首个请求完成、本请求拒绝；如需按冲突口径更新请使用重算"
                )
            if recompute and previous_version == 0:
                raise RetentionRejected(
                    f"策略 {strategy.get('策略编号')} 尚未装载过保留台账，不能直接重算"
                )
            version = previous_version + 1
            now = datetime.now()
            batch_no = f"B{now.strftime('%Y%m%d%H%M%S')}-{strategy_id}-V{version}"
            generated_at = now.strftime("%Y-%m-%d %H:%M:%S")

            # 纯计算：按去重键 + 时间窗口选唯一主保留项，并产出重复项引用与待修复清单。
            ledger, fix_items = build_ledger(
                strategy, records, version=version, batch_no=batch_no, generated_at=generated_at,
            )

            # 固定处理依据快照：刷新页面后台账仍与当时策略口径对得上，不随后续改策略漂移。
            basis_snapshot = {field: strategy.get(field) for field in LIST_FIELDS}
            for item in ledger:
                item["策略依据快照"] = basis_snapshot
                item["装载/重算说明"] = reason
            for item in fix_items:
                item["策略依据快照"] = basis_snapshot

            # 跨模块：血缘服务负责把台账、重复项引用、待修复清单持久化并回传核对结果。
            check = self.lineage.persist_retention(
                strategy=strategy, ledger=ledger, fix_items=fix_items,
                batch_no=batch_no, version=version,
            )
            return {
                "批次号": batch_no,
                "版本": version,
                "上一版本": previous_version,
                "策略编号": strategy.get("策略编号"),
                "输入来源记录数": len(records),
                "保留条目数": len(ledger),
                "重复项引用总数": sum(int(item.get("重复项数量", 0)) for item in ledger),
                "待修复条目数": len(fix_items),
                "核对结果": check,
                "台账": ledger,
                "待修复清单": fix_items,
            }
        finally:
            self.lineage.end_load(strategy_id)

    def load_retention(self, strategy_id: int) -> dict[str, Any]:
        strategy = store.find(MODULE, strategy_id)
        if strategy is None:
            raise RetentionRejected(f"去重策略 {strategy_id} 不存在或已归档")
        return self._compute_and_persist(
            strategy, recompute=False, reason="首次并发装载：仅首个请求完成并持久化保留台账",
        )

    def recompute_retention(self, strategy_id: int) -> dict[str, Any]:
        strategy = store.find(MODULE, strategy_id)
        if strategy is None:
            raise RetentionRejected(f"去重策略 {strategy_id} 不存在或已归档")
        # 按冲突口径重算，旧依据保留：历史版本台账不删除，新版本追加。
        return self._compute_and_persist(
            strategy, recompute=True,
            reason="按冲突口径重算：优先结果更符合业务规则，旧依据保留为历史版本",
        )

    # ------------------------------------------------------------------
    # 台账/清单/核对结果的只读查询（数据由血缘服务持久化，去重页只读展示）
    # ------------------------------------------------------------------
    def list_retention_ledger(self, strategy_id: int | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
        return self.lineage.list_retention_ledger(strategy_id=strategy_id, keyword=keyword)

    def list_fix_items(self, strategy_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return self.lineage.list_fix_items(strategy_id=strategy_id, status=status)

    def get_retention_detail(self, ledger_no: str) -> dict[str, Any] | None:
        for row in self.lineage.list_retention_ledger():
            if str(row.get("台账编号")) == ledger_no:
                return row
        return None

    def retention_summary(self) -> dict[str, Any]:
        return self.lineage.retention_summary()
