"""采集去重接口：维护去重策略，并提供保留记录台账装载、重算与回查。

新增链路接口（去重服务 → 血缘服务）：
- GET  /source-records            输入来源记录（去重计算的原始数据）
- POST /{id}/retention/load       并发装载保留台账（仅首个请求完成，其余 409 拒绝）
- POST /{id}/retention/recompute  按冲突口径重算，旧依据保留
- GET  /retention/ledger          保留记录台账及重复项引用（只读）
- GET  /retention/fix-list        待修复清单
- GET  /retention/summary         台账汇总指标
- GET  /retention/{ledger_no}     单个保留项明细（含被合并记录数据快照）
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.dedup import DedupService, RetentionRejected

router = APIRouter(prefix="/api/dedup", tags=["采集去重"])

service = DedupService()

LIST_FIELDS = ["策略编号", "输入来源", "去重键", "排序字段", "保留策略", "时间窗口", "重复阈值", "策略状态"]
STATUSES = ["草稿", "已配置", "生效中", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、已配置、生效中、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、策略编号关键字与状态过滤采集去重列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="dedup.csv"'})


# ----------------------------------------------------------------------
# 输入来源记录与保留台账（静态路径统一声明在 /{entry_id} 之前，避免被动态路由吞掉）
# ----------------------------------------------------------------------
@router.get("/source-records")
def list_source_records(strategy_id: int | None = Query(default=None)) -> dict:
    """去重输入来源记录：按去重键和时间窗口计算保留项时使用的原始采集数据。"""
    items = service.list_source_records(strategy_id)
    return {"items": items, "total": len(items)}


@router.get("/retention/summary")
def retention_summary() -> dict:
    """保留台账汇总：主保留记录数、重复项引用总数、待修复条目数等。"""
    return service.retention_summary()


@router.get("/retention/ledger")
def list_retention_ledger(
    strategy_id: int | None = Query(default=None, description="按策略 id 过滤"),
    keyword: str | None = Query(default=None, description="按去重键值/主保留记录/台账编号检索"),
) -> dict:
    """保留记录台账及重复项引用（血缘服务持久化，去重页只读）。"""
    items = service.list_retention_ledger(strategy_id=strategy_id, keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/retention/fix-list")
def list_fix_items(
    strategy_id: int | None = Query(default=None),
    status: str | None = Query(default=None, description="待修复、已修复"),
) -> dict:
    """窗口边界双口径冲突生成的待修复清单。"""
    items = service.list_fix_items(strategy_id=strategy_id, status=status)
    return {"items": items, "total": len(items)}


@router.get("/retention/{ledger_no}")
def get_retention_detail(ledger_no: str) -> dict:
    """单个保留项明细：主保留快照、窗口依据与被合并重复记录的数据快照，可逐项回查。"""
    item = service.get_retention_detail(ledger_no)
    if item is None:
        raise HTTPException(status_code=404, detail=f"保留台账 {ledger_no} 不存在或尚未装载")
    return item


@router.post("/{entry_id}/retention/load", response_model=ActionResult)
def load_retention(entry_id: int) -> ActionResult:
    """并发装载保留台账：同一策略仅首个请求完成并持久化，其余请求直接拒绝。"""
    try:
        result = service.load_retention(entry_id)
    except RetentionRejected as exc:
        # 并发冲突或无数据：业务可读失败，不静默成功。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    check = result["核对结果"]
    message = (
        f"保留台账已装载并由血缘服务持久化：版本 V{result['版本']}，"
        f"{result['保留条目数']} 条主保留记录、{result['重复项引用总数']} 条重复项引用、"
        f"{result['待修复条目数']} 条待修复；血缘核对{check.get('核对结果')}"
    )
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/{entry_id}/retention/recompute", response_model=ActionResult)
def recompute_retention(entry_id: int) -> ActionResult:
    """按冲突口径重算保留项，旧依据保留为历史版本，刷新后仍与处理依据对得上。"""
    try:
        result = service.recompute_retention(entry_id)
    except RetentionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResult(
        ok=True,
        message=f"已按冲突口径重算并生成版本 V{result['版本']}（旧版本 V{result['上一版本']} 保留）",
        entry=result,
    )


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条去重策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"去重策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条去重策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="去重策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条去重策略执行提交配置、启用策略、停用策略；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
