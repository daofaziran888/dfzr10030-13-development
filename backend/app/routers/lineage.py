"""数据血缘追踪 DAG 拓扑接口：返回节点、连线图结构。

新增保留台账血缘侧接口（血缘服务只持久化/只读核对，不修改去重台账）：
- GET /retention/ledger      血缘侧持久化的保留记录台账
- GET /retention/fix-list    待修复清单
- GET /retention/bindings    保留项 → 下游字段关系绑定
- GET /retention/lookup      按主保留记录/台账编号回查保留项与被合并快照
- GET /retention/summary     血缘侧台账汇总
- GET /retention/verify      只读核对结果（同一窗口唯一主记录、快照可回查）
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.lineage import LineageService

router = APIRouter(prefix="/api/lineage", tags=["数据血缘追踪"])
service = LineageService()
LIST_FIELDS = ["关系编号", "上游节点", "下游节点", "来源字段", "目标字段", "转换说明", "采集批次", "关系版本"]
STATUSES = ["草稿", "已记录", "已验证", "已归档"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def get_graph() -> dict[str, Any]:
    """返回节点与边组成的 DAG 图数据。"""
    return service.get_graph()


@router.get("/export")
def export_entries() -> StreamingResponse:
    nodes, edges = service.raw_data()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["type", "id", "name", "from", "to", "label", "status"])
    for node in nodes:
        writer.writerow(["node", node.get("id",""), csv_cell(node.get("name","")), "", "", "", node.get("status","")])
    for edge in edges:
        writer.writerow(["edge", "", "", edge.get("from",""), edge.get("to",""), edge.get("label",""), ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=lineage.csv"})


# ----------------------------------------------------------------------
# 保留记录台账血缘侧（静态路径声明在 /{entry_id} 之前）
# ----------------------------------------------------------------------
@router.get("/retention/summary")
def retention_summary() -> dict:
    return service.retention_summary()


@router.get("/retention/ledger")
def list_retention_ledger(
    strategy_id: int | None = Query(default=None),
    version: int | None = Query(default=None),
    keyword: str | None = Query(default=None),
) -> dict:
    """血缘服务持久化的保留记录台账及重复项引用（只读输入）。"""
    items = service.list_retention_ledger(strategy_id=strategy_id, version=version, keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/retention/fix-list")
def list_fix_items(
    strategy_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict:
    items = service.list_fix_items(strategy_id=strategy_id, status=status)
    return {"items": items, "total": len(items)}


@router.get("/retention/bindings")
def list_bindings(keyword: str | None = Query(default=None)) -> dict:
    """每个保留项绑定到的下游字段关系。"""
    items = service.list_bindings(keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/retention/lookup")
def lookup_retention(
    retention_no: str = Query(..., description="主保留记录编号或台账编号"),
) -> dict:
    """血缘页按保留项回查：保留项、重复项数据快照、下游字段绑定一次返回。"""
    result = service.lookup_by_retention(retention_no)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=result.get("message", "未找到保留项"))
    return result


@router.get("/retention/verify")
def verify_retention(
    strategy_id: int = Query(..., description="按去重策略 id 只读核对"),
    version: int | None = Query(default=None),
) -> dict:
    """血缘服务把台账作为只读输入核对，并向采集去重返回核对结果。"""
    return service.verify_ledger(strategy_id=strategy_id, version=version)


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"血缘关系 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="血缘关系已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
