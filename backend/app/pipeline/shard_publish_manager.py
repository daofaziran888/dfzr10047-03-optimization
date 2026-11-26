"""待发布分片管理器：逐片字段校验、领取范围冻结与接收回执幂等结算。

核心行为：
- 逐片校验：每份分片按自身必填列校验，整批列的并集不能代替逐片校验
- 领取冻结：提交确认单后领取的分片范围与判定口径一起冻结，事后补值不并入旧交付
- 回执幂等：同一接收凭据只结算一次，目标位置按行标识去重，不重复写入
- 剩余选择：写入中断后只继续未接收的行，已确认收到的行不重发
"""
from __future__ import annotations

from typing import Any

# 分片状态到「为什么不能再次发布」的说明
LOCKED_REASON = {
    "已领取": "已被交付领取，需在所属交付继续处理",
    "已送达": "已确认送达，只接受核对",
    "已核对": "已核对，只接受核对",
}


def split_csv(text: Any) -> list[str]:
    """把逗号分隔的字段串拆成去空白列表；已是列表时原样去空白。"""
    if isinstance(text, (list, tuple)):
        return [str(part).strip() for part in text if str(part).strip()]
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def join_csv(values: list[str]) -> str:
    return ",".join(values)


def as_int(text: Any, default: int = 0) -> int:
    text = str(text if text is not None else "").strip()
    return int(text) if text.isdigit() else default


class ShardPublishManager:
    """分片发布纯逻辑：不直接落库，由领域服务负责持久化。"""

    DEFAULT_MIN_SHARD_ROWS = 1

    # ------------------------------------------------------------------
    # 判定口径
    # ------------------------------------------------------------------
    def criteria_snapshot(self, contract: dict[str, Any],
                          snapshot_time: str) -> dict[str, Any]:
        """冻结当时的契约判定口径。

        交付与确认单都保存这份快照；之后契约阈值变更只影响新的提交，
        不会改写旧交付的结论。
        """
        return {
            "输出列集合": split_csv(contract.get("输出列集合")),
            "列顺序约定": split_csv(contract.get("列顺序约定")),
            "必填列": split_csv(contract.get("必填列")),
            "分片行数下限": as_int(
                contract.get("分片行数下限"), self.DEFAULT_MIN_SHARD_ROWS),
            "整批行数下限": as_int(contract.get("行数下限"), 0),
            "契约编号": str(contract.get("契约编号") or ""),
            "契约状态": str(contract.get("status") or ""),
            "契约校验时间": str(contract.get("校验时间") or ""),
            "快照时间": snapshot_time,
        }

    def scope_from_criteria(self, criteria: dict[str, Any]) -> dict[str, Any]:
        """把冻结口径整理成逐片校验直接可读的范围。"""
        return {
            "输出列集合": split_csv(criteria.get("输出列集合")),
            "列顺序约定": split_csv(criteria.get("列顺序约定")),
            "必填列": split_csv(criteria.get("必填列")),
            "分片行数下限": as_int(
                criteria.get("分片行数下限"), self.DEFAULT_MIN_SHARD_ROWS),
            "整批行数下限": as_int(criteria.get("整批行数下限"), 0),
            "契约编号": str(criteria.get("契约编号") or ""),
        }

    # ------------------------------------------------------------------
    # 逐片校验
    # ------------------------------------------------------------------
    def shard_columns(self, shard: dict[str, Any]) -> list[str]:
        return split_csv(shard.get("分片列"))

    def shard_rows(self, shard: dict[str, Any]) -> list[str]:
        """分片行标识：显式登记优先，否则按行数补出稳定标识。"""
        explicit = split_csv(shard.get("行标识"))
        if explicit:
            return explicit
        count = as_int(shard.get("行数"), 0)
        shard_no = str(shard.get("分片编号") or "SH")
        return [f"{shard_no}:{index}" for index in range(1, count + 1)]

    def required_columns(self, shard: dict[str, Any],
                         scope: dict[str, Any]) -> list[str]:
        """分片必填列：分片可声明更严的必填列，否则继承判定口径的必填列。"""
        override = split_csv(shard.get("必填列"))
        return override or list(scope.get("必填列") or [])

    def validate_shard(self, shard: dict[str, Any], scope: dict[str, Any],
                       criteria: dict[str, Any], check_time: str) -> dict[str, Any]:
        """按分片自身字段校验，返回可发/缺列结论。

        校验顺序：分片是否已进入交付 → 必填列 → 越界列 → 行数下限。
        整批输出列的并集只用于判断越界，不用于替代逐片必填列校验。
        """
        columns = self.shard_columns(shard)
        rows = self.shard_rows(shard)
        required = self.required_columns(shard, scope)
        contract_scope = list(scope.get("输出列集合") or [])
        min_rows = as_int(
            scope.get("分片行数下限"), self.DEFAULT_MIN_SHARD_ROWS)

        missing = [column for column in required if column not in columns]
        extra = [column for column in columns if column not in contract_scope]
        failures: list[str] = []
        if missing:
            failures.append(f"缺失必填列 {missing}")
        if extra:
            failures.append(f"存在契约未约定列 {extra}")
        if len(rows) < min_rows:
            failures.append(f"分片行数 {len(rows)} 低于下限 {min_rows}")
        if not rows:
            failures.append("分片没有可交付行")

        status = str(shard.get("status") or "")
        blocked_reason = LOCKED_REASON.get(status, "")
        publishable = not failures and not blocked_reason

        if blocked_reason:
            conclusion = f"不可再发布：{blocked_reason}"
        elif failures:
            conclusion = ";".join(failures)
        else:
            conclusion = (
                f"可发布：{len(rows)} 行满足必填列 "
                f"{required if required else '（无）'}")

        return {
            "分片编号": str(shard.get("分片编号") or ""),
            "关联契约": str(shard.get("关联契约") or ""),
            "接收目标": str(shard.get("接收目标") or ""),
            "分片列": columns,
            "行数": len(rows),
            "行标识": rows,
            "必填列": required,
            "缺失必填列": missing,
            "未约定列": extra,
            "可发": publishable,
            "结论": conclusion,
            "分片状态": status or "待发布",
            "所属交付": str(shard.get("所属交付") or ""),
            "所属确认单": str(shard.get("所属确认单") or ""),
            "判定口径": dict(criteria),
            "校验时间": check_time,
        }

    # ------------------------------------------------------------------
    # 确认单与领取范围
    # ------------------------------------------------------------------
    def decision_for(self, line: dict[str, Any]) -> str:
        if line.get("可发"):
            return "可发"
        if line.get("缺失必填列"):
            return "缺列"
        return "不可发"

    def frozen_scope(self, lines: list[dict[str, Any]], target: str,
                     shard_numbers: list[str]) -> list[dict[str, Any]]:
        """从确认单行冻结本次交付的分片范围（列、行数、行标识）。"""
        wanted = set(shard_numbers)
        scope: list[dict[str, Any]] = []
        for line in lines:
            if line.get("分片编号") not in wanted:
                continue
            if target and line.get("接收目标") != target:
                continue
            scope.append({
                "分片编号": line["分片编号"],
                "分片列": list(line.get("分片列") or []),
                "行数": as_int(line.get("行数"), 0),
                "行标识": list(line.get("行标识") or []),
                "接收目标": line.get("接收目标", target),
            })
        return scope

    def scope_rows(self, scope: list[dict[str, Any]]) -> dict[str, list[str]]:
        return {
            str(item.get("分片编号")): list(item.get("行标识") or [])
            for item in scope
        }

    def remaining_selection(self, scope: list[dict[str, Any]],
                            received: dict[str, list[str]]) -> dict[str, list[str]]:
        """可恢复的剩余选择：冻结范围内尚未接收的行，已接收的行不重发。"""
        remaining: dict[str, list[str]] = {}
        for item in scope:
            shard_no = str(item.get("分片编号"))
            already = set(received.get(shard_no, []))
            rows = [row for row in item.get("行标识", []) if row not in already]
            if rows:
                remaining[shard_no] = rows
        return remaining

    def merge_received(self, received: dict[str, list[str]],
                       written: dict[str, list[str]]) -> dict[str, list[str]]:
        merged: dict[str, list[str]] = {
            key: list(value) for key, value in received.items()
        }
        for shard_no, rows in written.items():
            bucket = merged.setdefault(shard_no, [])
            for row in rows:
                if row not in bucket:
                    bucket.append(row)
        return merged

    def validate_requested_rows(self, remaining: dict[str, list[str]],
                                requested: dict[str, list[str]],
                                received: dict[str, list[str]] | None = None
                                ) -> tuple[bool, str]:
        """本次声明的接收行必须落在剩余选择或已接收行内。

        已接收的行允许随回执重放出现，由写入计划跳过，不会重复落到目标位置；
        既不在剩余选择、也从未接收过的行属于越界声明，直接拒绝。
        """
        received = received or {}
        for shard_no, rows in requested.items():
            allowed = set(remaining.get(shard_no, []))
            allowed |= set(received.get(shard_no, []))
            if not allowed:
                return False, f"分片 {shard_no} 没有可接收的剩余行"
            outside = [row for row in rows if row not in allowed]
            if outside:
                return False, (
                    f"分片 {shard_no} 的行 {outside} 不在剩余选择内，"
                    f"不属于本次交付的分片范围")
        return True, ""

    def plan_writes(self, remaining: dict[str, list[str]],
                    requested: dict[str, list[str]] | None,
                    target_rows: dict[str, set[str]],
                    received: dict[str, list[str]] | None = None
                    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """计算真正需要写入目标位置的行：剩余选择内且目标位置尚未存在。"""
        write_map = remaining if requested is None else requested
        received = received or {}
        written: dict[str, list[str]] = {}
        skipped: dict[str, list[str]] = {}
        for shard_no, rows in write_map.items():
            already_received = set(received.get(shard_no, []))
            for row in rows:
                if (row in target_rows.get(shard_no, set())
                        or row in already_received
                        or row not in remaining.get(shard_no, [])):
                    skipped.setdefault(shard_no, []).append(row)
                    continue
                written.setdefault(shard_no, []).append(row)
        return written, skipped

    # ------------------------------------------------------------------
    # 回执结算
    # ------------------------------------------------------------------
    def receipt_status(self, fully_received: bool, all_shards_qualified: bool,
                       interrupted: bool) -> str:
        if fully_received and not interrupted and all_shards_qualified:
            return "已接收"
        return "已中断"

    def shard_received_counts(self, scope: list[dict[str, Any]],
                              received: dict[str, list[str]]) -> list[dict[str, Any]]:
        """逐片汇总已收/总行数，供页面展示分片回执。"""
        summary: list[dict[str, Any]] = []
        for item in scope:
            shard_no = str(item.get("分片编号"))
            targets = item.get("行标识") or []
            target_set = set(targets)
            got = [row for row in received.get(shard_no, []) if row in target_set]
            summary.append({
                "分片编号": shard_no,
                "总行数": len(targets),
                "已接收行数": len(got),
                "是否收齐": len(got) >= len(targets),
                "分片列": list(item.get("分片列") or []),
            })
        return summary

    def remaining_rows(self, remaining: dict[str, list[str]]) -> int:
        return sum(len(rows) for rows in remaining.values())

    # ------------------------------------------------------------------
    # 旧交付回查
    # ------------------------------------------------------------------
    def criteria_changes(self, frozen: dict[str, Any],
                         current: dict[str, Any]) -> list[str]:
        """比较冻结口径与当前契约口径，列出变更项。"""
        changed: list[str] = []
        for key in ("输出列集合", "必填列", "分片行数下限", "整批行数下限"):
            if frozen.get(key) != current.get(key):
                changed.append(key)
        return changed

    def conclude(self, frozen: dict[str, Any], current: dict[str, Any],
                 scope: list[dict[str, Any]]) -> dict[str, Any]:
        """旧交付回查结论：沿用冻结范围与口径，阈值变更不改旧结论。"""
        changes = self.criteria_changes(frozen, current)
        return {
            "冻结分片": [str(item.get("分片编号")) for item in scope],
            "冻结行数": sum(as_int(item.get("行数"), 0) for item in scope),
            "冻结必填列": list(frozen.get("必填列") or []),
            "冻结分片行数下限": as_int(
                frozen.get("分片行数下限"), self.DEFAULT_MIN_SHARD_ROWS),
            "冻结时间": frozen.get("快照时间", ""),
            "当前必填列": list(current.get("必填列") or []),
            "当前分片行数下限": as_int(
                current.get("分片行数下限"), self.DEFAULT_MIN_SHARD_ROWS),
            "口径变更项": changes,
            "沿用冻结口径": True,
            "说明": (
                "契约口径自本次交付后已变更 " + ",".join(changes)
                + "，旧交付仍按冻结口径回查，不改写当时结论"
                if changes else
                "契约口径与冻结口径一致，旧交付按当时范围与结论回查"
            ),
        }
