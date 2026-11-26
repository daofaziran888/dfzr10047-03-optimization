"""待发布分片路由：分片校验回执、确认单编制/提交、接收回执、继续处理与核对。

这些接口不能落进通用 /api/{module} 资源路由：确认单与交付是跨模块编排动作，
每次提交都要在同一份冻结口径上逐片复核，不能靠单表状态流转表达。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    ShardReceivePayload,
    ShardSheetPayload,
    ShardSubmitPayload,
)
from app.services.shard_publish import service

router = APIRouter(prefix="/api/shard-publish", tags=["待发布分片"])


def _result(ok: bool, message: str, entry: Any = None) -> ActionResult:
    return ActionResult(ok=ok, message=message, entry=entry)


@router.get("/shards")
def list_shards(contract: str = Query(default="")) -> dict[str, Any]:
    """待发布分片清单：展示列、行标识与所属交付。"""
    rows = service.shard_overview(contract)
    return {"items": rows, "total": len(rows)}


@router.get("/receipt")
def shard_receipt(contract: str, target: str = Query(default="")) -> ActionResult:
    """按接收目标给出分片校验回执：哪些片可发、哪些片缺列。"""
    ok, message, payload = service.shard_receipt_by_target(contract, target)
    return _result(ok, message, payload)


@router.post("/supplement")
def supplement_shard(payload: dict[str, Any]) -> ActionResult:
    """补齐分片字段：只允许仍处于待发布的分片。"""
    shard_no = str(payload.get("shard_no") or "").strip()
    ok, message = service.shards.supplement(
        shard_no,
        columns=payload.get("columns"),
        add_columns=payload.get("add_columns"),
        required_columns=payload.get("required_columns"),
    )
    return _result(ok, message, service.shards.find(shard_no))


@router.get("/sheets")
def list_sheets(contract: str = Query(default="")) -> dict[str, Any]:
    rows = service.list_sheets(contract)
    return {"items": rows, "total": len(rows)}


@router.get("/sheets/{sheet_no}")
def get_sheet(sheet_no: str) -> dict[str, Any]:
    detail = service.sheet_detail(sheet_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"确认单 {sheet_no} 不存在")
    return detail


@router.post("/sheets")
def build_sheet(payload: ShardSheetPayload) -> ActionResult:
    """编制确认单：同一张单上逐片核对可发与缺列。"""
    ok, message, sheet = service.build_confirm_sheet(
        payload.contract_no, payload.target, payload.shard_numbers,
        payload.operator)
    return _result(ok, message, sheet)


@router.post("/sheets/{sheet_no}/submit")
def submit_sheet(sheet_no: str, payload: ShardSubmitPayload) -> ActionResult:
    """提交确认单：领取已具备单独交付资格的分片，缺列片不并入。"""
    ok, message, delivery = service.submit_confirm_sheet(
        sheet_no, payload.operator, payload.shard_numbers)
    return _result(ok, message, delivery)


@router.get("/deliveries")
def list_deliveries(contract: str = Query(default=""),
                    status: str = Query(default="")) -> dict[str, Any]:
    rows = service.list_deliveries(contract, status)
    return {"items": rows, "total": len(rows)}


@router.get("/deliveries/{delivery_no}")
def get_delivery(delivery_no: str) -> dict[str, Any]:
    detail = service.delivery_detail(delivery_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"交付 {delivery_no} 不存在")
    return detail


@router.post("/deliveries/{delivery_no}/continue")
def continue_delivery(delivery_no: str) -> ActionResult:
    """未送达的交付继续处理：沿用可恢复的剩余选择。"""
    ok, message, detail = service.continue_delivery(delivery_no)
    return _result(ok, message, detail)


@router.post("/deliveries/{delivery_no}/receive")
def receive_delivery(delivery_no: str, payload: ShardReceivePayload) -> ActionResult:
    """结算接收回执：同一凭据只算一次，目标位置不重复写入。"""
    ok, message, detail = service.receive(
        delivery_no, payload.receipt_token, payload.written,
        payload.interrupted, payload.operator)
    return _result(ok, message, detail)


@router.post("/deliveries/{delivery_no}/verify")
def verify_delivery(delivery_no: str) -> ActionResult:
    """已送达交付只接受核对。"""
    ok, message, detail = service.verify_delivery(delivery_no)
    return _result(ok, message, detail)


@router.get("/target-rows")
def target_rows(target: str = Query(default="")) -> dict[str, Any]:
    """目标位置实际接收行：用于核对目的地不会多出原有行。"""
    rows = service.target_rows(target)
    return {"items": rows, "total": len(rows)}
