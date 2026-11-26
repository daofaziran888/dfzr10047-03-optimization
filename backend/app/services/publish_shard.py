"""待发布分片领域服务：维护分片列与行标识，按契约逐片校验并冻结交付范围，产出分片数据集。"""
from __future__ import annotations

from typing import Any

from app.pipeline.shard_publish_manager import (
    ShardPublishManager,
    split_csv,
    join_csv,
)
from app.services.base import BaseModuleService
from app.store import store


class PublishShardService(BaseModuleService):
    MODULE = "publish_shard"
    ENTITY = "待发布分片"
    OPERATION_TERM = "分片"
    LIST_FIELDS = ["分片编号", "关联契约", "接收目标", "分片列", "必填列", "行数", "行标识", "所属交付"]
    REQUIRED_FIELDS = ["分片编号", "关联契约", "接收目标"]
    STATUS_ORDER = ["待发布", "已领取", "待补收", "已送达", "已核对"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def __init__(self) -> None:
        self.manager = ShardPublishManager()

    def _find(self, shard_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("分片编号") == shard_no:
                return row
        return None

    def find(self, shard_no: str) -> dict[str, Any] | None:
        return self._find(shard_no)

    def by_contract(self, contract_no: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(self.MODULE)
            if row.get("关联契约") == contract_no
        ]

    def supplement(self, shard_no: str, columns: list[str] | None = None,
                   add_columns: list[str] | None = None,
                   required_columns: list[str] | None = None) -> tuple[bool, str]:
        """补齐分片字段：只有仍处于待发布的分片允许改写列。

        已被领取的分片范围已经冻结，后到的补值不能改写旧交付的接收行，
        只能在新的提交里另行组成交付。
        """
        shard = self._find(shard_no)
        if shard is None:
            return False, f"分片 {shard_no} 不存在"
        if shard.get("status") != "待发布":
            return False, (
                f"分片 {shard_no} 当前状态为 {shard.get('status')}，"
                f"已被交付领取的分片不得改写字段"
            )

        current = split_csv(shard.get("分片列"))
        if columns is not None:
            current = [column.strip() for column in columns if str(column).strip()]
        if add_columns:
            for column in add_columns:
                name = str(column).strip()
                if name and name not in current:
                    current.append(name)
        if required_columns is not None:
            shard["必填列"] = join_csv(
                [column.strip() for column in required_columns if str(column).strip()])
        shard["分片列"] = join_csv(current)
        return True, f"分片 {shard_no} 已补齐字段：{shard['分片列']}"

    def validation_rows(self, contract: dict[str, Any],
                        check_time: str) -> list[dict[str, Any]]:
        """按契约当前口径逐片校验全部待发布相关分片。"""
        criteria = self.manager.criteria_snapshot(contract, check_time)
        scope = self.manager.scope_from_criteria(criteria)
        lines: list[dict[str, Any]] = []
        for shard in self.by_contract(str(contract.get("契约编号") or "")):
            line = self.manager.validate_shard(shard, scope, criteria, check_time)
            line["判定结论"] = self.manager.decision_for(line)
            lines.append(line)
        return lines

    def mark_claimed(self, shard_no: str, delivery_no: str) -> None:
        shard = self._find(shard_no)
        if shard is None:
            return
        shard["status"] = "已领取"
        shard["pending"] = True
        shard["所属交付"] = delivery_no

    def mark_progress(self, shard_no: str, status: str) -> None:
        shard = self._find(shard_no)
        if shard is None:
            return
        shard["status"] = status
        shard["pending"] = status in ("已领取", "待补收")
        shard["abnormal"] = status == "待补收"
