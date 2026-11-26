"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


# ---- 待发布分片工作流：确认单、领取、接收回执 ----

class ShardSheetPayload(BaseModel):
    """编制确认单：选定契约、接收目标与待核对的候选分片。"""
    contract_no: str
    target: str = ""
    shard_numbers: list[str] = Field(default_factory=list)
    operator: str = ""


class ShardSubmitPayload(BaseModel):
    """提交确认单：可只领取其中已合格的分片。"""
    operator: str = ""
    shard_numbers: list[str] | None = None


class ShardReceivePayload(BaseModel):
    """接收回执：接收凭据用于幂等；written 为空表示写入全部剩余选择。"""
    receipt_token: str
    written: dict[str, list[str]] | None = None
    interrupted: bool = False
    operator: str = ""

