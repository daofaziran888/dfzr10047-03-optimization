"""分片发布领域服务：编制确认单、冻结交付范围、结算接收回执并关联投递台账，产出分片交付数据集。"""
from __future__ import annotations

from typing import Any

from app.pipeline.shard_publish_manager import (
    ShardPublishManager,
    as_int,
    join_csv,
    split_csv,
)
from app.services.publish_shard import PublishShardService
from app.store import store

SHEET_OPEN = "已编制"
SHEET_SUBMITTED = "已提交"
SHEET_VOID = "已作废"

DELIVERY_WAITING = "待接收"
DELIVERY_RECEIVED = "已接收"
DELIVERY_INTERRUPTED = "已中断"


class ShardPublishService:
    """分片发布编排：确认单 → 领取 → 接收回执 → 台账关联。"""

    ENTITY = "分片交付"

    def __init__(self) -> None:
        self.manager = ShardPublishManager()
        self.shards = PublishShardService()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _next_id(module: str) -> int:
        rows = store.rows(module)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    @staticmethod
    def _sequence(module: str, field: str, prefix: str) -> str:
        used = {str(row.get(field) or "") for row in store.rows(module)}
        index = 1
        while f"{prefix}{index:04d}" in used:
            index += 1
        return f"{prefix}{index:04d}"

    def _contract(self, contract_no: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows("output_contract")
             if row.get("契约编号") == contract_no), None)

    def _sheet(self, sheet_no: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows("confirm_sheet")
             if row.get("确认单编号") == sheet_no), None)

    def _delivery(self, delivery_no: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows("publish_delivery")
             if row.get("交付编号") == delivery_no), None)

    def _receipts(self, delivery_no: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows("publish_receipt")
            if row.get("关联交付") == delivery_no
        ]

    def _ledger(self, delivery_no: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows("result_deliver")
             if row.get("投递记录编号") == f"RD-{delivery_no}"), None)

    def _require_publishable_contract(
            self, contract_no: str) -> tuple[dict[str, Any] | None, str]:
        contract = self._contract(contract_no)
        if contract is None:
            return None, f"契约 {contract_no} 不存在，不得编制分片确认单"
        if contract.get("status") != "已通过":
            return None, (
                f"契约 {contract_no} 状态为 {contract.get('status')}，"
                f"只有已通过的契约才能按分片发布")
        return contract, ""

    # ------------------------------------------------------------------
    # 分片校验回执（按接收目标）
    # ------------------------------------------------------------------
    def shard_receipt_by_target(self, contract_no: str,
                                target: str = "") -> tuple[bool, str, dict[str, Any]]:
        """按接收目标给出分片校验回执：哪些片可发、哪些片缺列。"""
        contract, error = self._require_publishable_contract(contract_no)
        if contract is None:
            return False, error, {}
        now = _now()
        criteria = self.manager.criteria_snapshot(contract, now)
        scope = self.manager.scope_from_criteria(criteria)

        shards = [
            shard for shard in self.shards.by_contract(contract_no)
            if not target or shard.get("接收目标") == target
        ]
        if not shards:
            return False, (
                f"契约 {contract_no} 在接收目标 {target or '（全部）'} 下没有待发布分片"
            ), {}

        lines = []
        for shard in shards:
            line = self._evaluate_shard(shard, scope, criteria, target, now)
            lines.append(line)

        targets: dict[str, list[dict[str, Any]]] = {}
        for line in lines:
            targets.setdefault(line["接收目标"], []).append(line)

        return True, "分片校验完成", {
            "关联契约": contract_no,
            "判定口径": criteria,
            "接收目标": [
                {
                    "接收目标": name,
                    "分片": target_lines,
                    "可发分片": [l["分片编号"] for l in target_lines if l["可发"]],
                    "缺列分片": [l["分片编号"] for l in target_lines
                                if l["缺失必填列"]],
                    "不可发分片": [l["分片编号"] for l in target_lines
                                  if not l["可发"]],
                }
                for name, target_lines in targets.items()
            ],
            "校验时间": now,
        }

    def _remember_verdict(self, shard: dict[str, Any],
                          line: dict[str, Any]) -> None:
        shard["最近校验结论"] = (
            f"{line['结论']}（{line['校验时间']}）")
        shard["最近校验时间"] = line["校验时间"]

    def _evaluate_shard(self, shard: dict[str, Any], scope: dict[str, Any],
                        criteria: dict[str, Any], target: str,
                        now: str) -> dict[str, Any]:
        """按冻结口径校验单片，并把接收目标也纳入可发判定。"""
        line = self.manager.validate_shard(shard, scope, criteria, now)
        line["判定结论"] = self.manager.decision_for(line)
        if target and line["接收目标"] != target:
            line["可发"] = False
            line["判定结论"] = "目标不符"
            line["结论"] = (
                f"分片接收目标为 {line['接收目标']}，"
                f"不属于本次接收目标 {target}")
        self._remember_verdict(shard, line)
        return line

    # ------------------------------------------------------------------
    # 编制确认单
    # ------------------------------------------------------------------
    def build_confirm_sheet(self, contract_no: str, target: str,
                            shard_numbers: list[str], operator: str,
                            now: str | None = None) -> tuple[
                                bool, str, dict[str, Any] | None]:
        """编制确认单：在同一张单上逐片核对可发与缺列，并冻结判定口径。"""
        contract, error = self._require_publishable_contract(contract_no)
        if contract is None:
            return False, error, None
        if not shard_numbers:
            return False, "确认单至少要选择一份分片", None
        now = now or _now()

        known = {shard.get("分片编号"): shard
                 for shard in self.shards.by_contract(contract_no)}
        unknown = [no for no in shard_numbers if no not in known]
        if unknown:
            return False, f"分片 {unknown} 不属于契约 {contract_no}", None

        criteria = self.manager.criteria_snapshot(contract, now)
        scope = self.manager.scope_from_criteria(criteria)
        lines = [
            self._evaluate_shard(known[shard_no], scope, criteria, target, now)
            for shard_no in shard_numbers
        ]

        sheet_no = self._sequence("confirm_sheet", "确认单编号", "CS-")
        sheet = {
            "id": self._next_id("confirm_sheet"),
            "status": SHEET_OPEN, "pending": True, "abnormal": False,
            "确认单编号": sheet_no,
            "关联契约": contract_no,
            "接收目标": target,
            "操作人": operator,
            "编制时间": now,
            "提交时间": "",
            "分片范围": join_csv(shard_numbers),
            "可发分片": join_csv([l["分片编号"] for l in lines if l["可发"]]),
            "缺列分片": join_csv([l["分片编号"] for l in lines
                                  if not l["可发"]]),
            "判定口径": criteria,
            "行明细": lines,
            "所属交付": "",
        }
        store.rows("confirm_sheet").append(sheet)
        publishable = [l["分片编号"] for l in lines if l["可发"]]
        if publishable:
            summary = f"可发 {len(publishable)} 片：{', '.join(publishable)}"
        else:
            summary = "没有可发布分片"
        blocked = [l["分片编号"] for l in lines if not l["可发"]]
        if blocked:
            summary += f"；缺列/不可发：{', '.join(blocked)}"
        return True, f"确认单 {sheet_no} 已编制，{summary}", dict(sheet)

    # ------------------------------------------------------------------
    # 提交（领取）确认单
    # ------------------------------------------------------------------
    def submit_confirm_sheet(self, sheet_no: str, operator: str = "",
                             shard_numbers: list[str] | None = None,
                             now: str | None = None) -> tuple[
                                 bool, str, dict[str, Any] | None]:
        """提交确认单：服务端按分片实际字段复核后领取，范围与口径一并冻结。

        选中但当前仍缺列的分片不会并入本次交付；已在本交付冻结范围外的补值
        只能在新的确认单里另行交付，避免改写已领取交付的接收行。
        """
        sheet = self._sheet(sheet_no)
        if sheet is None:
            return False, f"确认单 {sheet_no} 不存在", None
        if sheet.get("status") != SHEET_OPEN:
            return False, (
                f"确认单 {sheet_no} 当前状态为 {sheet.get('status')}，"
                f"已提交的确认单不得重复领取"), None
        now = now or _now()

        contract_no = str(sheet.get("关联契约") or "")
        contract = self._contract(contract_no)
        if contract is None:
            return False, f"契约 {contract_no} 不存在", None

        criteria = dict(sheet.get("判定口径") or {})
        if now:
            criteria["快照时间"] = now
        scope = self.manager.scope_from_criteria(criteria)
        selected = (
            shard_numbers
            if shard_numbers is not None
            else [l["分片编号"] for l in sheet.get("行明细", [])]
        )
        sheet_shards = {l["分片编号"]: l for l in sheet.get("行明细", [])}
        extras = [no for no in selected if no not in sheet_shards]
        if extras:
            return False, f"分片 {extras} 不在确认单 {sheet_no} 的分片范围内", None

        accepted: list[dict[str, Any]] = []
        rejected: list[str] = []
        target = str(sheet.get("接收目标") or "")
        for shard_no in selected:
            shard = self.shards.find(shard_no)
            if shard is None:
                rejected.append(f"{shard_no}（不存在）")
                continue
            line = self._evaluate_shard(shard, scope, criteria, target, now)
            sheet_shards[shard_no] = line
            if line["可发"]:
                accepted.append(line)
            else:
                rejected.append(f"{shard_no}（{line['结论']}）")

        if not accepted:
            sheet["行明细"] = list(sheet_shards.values())
            return False, (
                f"确认单 {sheet_no} 选中的分片均不可发布："
                f"{'；'.join(rejected)}"), None

        # 创建交付并冻结范围与口径
        delivery_no = self._sequence("publish_delivery", "交付编号", "DL-")
        frozen = self.manager.frozen_scope(
            accepted, str(sheet.get("接收目标") or ""),
            [line["分片编号"] for line in accepted])
        total_rows = sum(as_int(item.get("行数"), 0) for item in frozen)
        delivery = {
            "id": self._next_id("publish_delivery"),
            "status": DELIVERY_WAITING, "pending": True, "abnormal": False,
            "交付编号": delivery_no,
            "关联确认单": sheet_no,
            "关联契约": contract_no,
            "接收目标": sheet.get("接收目标", ""),
            "领取人": operator or str(sheet.get("操作人") or ""),
            "领取时间": now,
            "分片范围": join_csv([item["分片编号"] for item in frozen]),
            "冻结范围": frozen,
            "冻结判定": [
                {
                    "分片编号": line["分片编号"],
                    "分片列": line["分片列"],
                    "必填列": line["必填列"],
                    "行数": line["行数"],
                    "结论": line["结论"],
                    "校验时间": line["校验时间"],
                }
                for line in accepted
            ],
            "判定口径": criteria,
            "口径变更提示": self._criteria_drift(contract, criteria, now),
            "总行数": str(total_rows),
            "已接收行数": "0",
            "剩余选择": self.manager.scope_rows(frozen),
            "接收回执": "",
            "最后接收时间": "",
        }
        store.rows("publish_delivery").append(delivery)

        for item in frozen:
            self.shards.mark_claimed(
                str(item["分片编号"]), delivery_no)
            shard = self.shards.find(str(item["分片编号"]))
            if shard is not None:
                shard["所属确认单"] = sheet_no

        self._upsert_ledger(delivery)

        remaining = [no for no in selected
                     if no not in {item["分片编号"] for item in frozen}]
        sheet["status"] = SHEET_SUBMITTED
        sheet["pending"] = False
        sheet["已提交分片"] = join_csv([item["分片编号"] for item in frozen])
        sheet["未并入分片"] = join_csv(remaining)
        sheet["提交时间"] = now
        sheet["所属交付"] = delivery_no
        sheet["可发分片"] = join_csv(
            [l["分片编号"] for l in sheet_shards.values() if l.get("可发")])
        sheet["缺列分片"] = join_csv(
            [l["分片编号"] for l in sheet_shards.values()
             if l.get("缺失必填列")])
        sheet["行明细"] = list(sheet_shards.values())

        message = (
            f"确认单 {sheet_no} 已提交：领取分片 "
            f"{delivery['分片范围']}（{total_rows} 行）→ {delivery['接收目标']}")
        if remaining:
            message += f"；未并入本次交付：{', '.join(remaining)}"
        if delivery["口径变更提示"]:
            message += f"；{delivery['口径变更提示']}"
        return True, message, self.delivery_detail(delivery_no)

    def _criteria_drift(self, contract: dict[str, Any],
                        frozen: dict[str, Any], now: str) -> str:
        current = self.manager.criteria_snapshot(contract, now)
        changed = self.manager.criteria_changes(frozen, current)
        if not changed:
            return ""
        return f"契约口径自编制后已变更 {changed}，本次交付沿用确认单冻结口径"

    # ------------------------------------------------------------------
    # 投递台账
    # ------------------------------------------------------------------
    def _upsert_ledger(self, delivery: dict[str, Any]) -> dict[str, Any]:
        """把交付的分片身份与已验证字段范围写入结果投递台账。"""
        criteria = dict(delivery.get("判定口径") or {})
        fields = join_csv(list(criteria.get("输出列集合") or []))
        record_no = f"RD-{delivery['交付编号']}"
        ledger = self._ledger(str(delivery["交付编号"]))
        if ledger is None:
            ledger = {
                "id": self._next_id("result_deliver"),
                "status": "投递中", "pending": True, "abnormal": False,
                "投递记录编号": record_no,
                "关联契约": delivery.get("关联契约", ""),
                "目标位置": delivery.get("接收目标", ""),
                "投递行数": delivery.get("总行数", "0"),
                "投递格式": "分片",
                "重试次数": "0",
                "投递结论": "",
                "投递时间": "",
                "关联交付": delivery["交付编号"],
                "分片身份": delivery.get("分片范围", ""),
                "已验证字段范围": fields,
                "接收回执": "",
                "已接收行数": "0",
                "接收分片行数": "",
            }
            store.rows("result_deliver").append(ledger)
        else:
            ledger["关联契约"] = delivery.get("关联契约", "")
            ledger["目标位置"] = delivery.get("接收目标", "")
            ledger["投递行数"] = delivery.get("总行数", "0")
            ledger["分片身份"] = delivery.get("分片范围", "")
            ledger["已验证字段范围"] = fields
        return ledger

    # ------------------------------------------------------------------
    # 接收回执
    # ------------------------------------------------------------------
    def receive(self, delivery_no: str, receipt_token: str,
                written: dict[str, list[str]] | None = None,
                interrupted: bool = False, operator: str = "",
                now: str | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        """结算一次接收回执：同一凭据只算一次，已接收行不重复写入目标位置。"""
        delivery = self._delivery(delivery_no)
        if delivery is None:
            return False, f"交付 {delivery_no} 不存在", None
        if not receipt_token:
            return False, "接收凭据不能为空", None
        now = now or _now()

        existing = next(
            (row for row in store.rows("publish_receipt")
             if row.get("接收凭据") == receipt_token), None)
        if existing is not None:
            if existing.get("关联交付") == delivery_no:
                return True, (
                    f"接收凭据 {receipt_token} 已结算"
                    f"（回执 {existing.get('回执编号')}），重放不重复写入"), \
                    self.delivery_detail(delivery_no)
            return False, (
                f"接收凭据 {receipt_token} 已用于交付 "
                f"{existing.get('关联交付')}，不能跨交付重复结算"), None

        if delivery.get("status") == DELIVERY_RECEIVED:
            return False, (
                f"交付 {delivery_no} 已全部接收，只接受核对"), None

        remaining = {
            str(shard): list(rows)
            for shard, rows in dict(delivery.get("剩余选择") or {}).items()
        }
        already_received = {
            str(shard): list(rows)
            for shard, rows in dict(delivery.get("已接收明细") or {}).items()
        }
        if not remaining:
            return False, f"交付 {delivery_no} 没有剩余可接收行", None

        if written is not None:
            if not written and not interrupted:
                return False, (
                    "本次回执未声明任何接收行；若不慎写空，请以 interrupted 标记登记中断"
                ), None
            ok, error = self.manager.validate_requested_rows(
                remaining, written, already_received)
            if not ok:
                return False, error, None

        target_rows = self._target_rows(delivery)
        to_write, skipped = self.manager.plan_writes(
            remaining, written, target_rows, already_received)

        written_rows: dict[str, list[str]] = {}
        for shard_no, rows in to_write.items():
            for row in rows:
                store.rows("publish_target_row").append({
                    "id": self._next_id("publish_target_row"),
                    "接收目标": delivery.get("接收目标", ""),
                    "分片编号": shard_no,
                    "行标识": row,
                    "交付编号": delivery_no,
                    "写入时间": now,
                })
            written_rows[shard_no] = list(rows)

        received = self.manager.merge_received(
            dict(delivery.get("已接收明细") or {}), written_rows)
        new_remaining = self.manager.remaining_selection(
            list(delivery.get("冻结范围") or []), received)
        fully = self.manager.remaining_rows(new_remaining) == 0

        receipt_no = self._sequence("publish_receipt", "回执编号", "RC-")
        receipt = {
            "id": self._next_id("publish_receipt"),
            "status": "已接收" if fully and not interrupted else "已中断",
            "pending": not (fully and not interrupted),
            "abnormal": interrupted or not fully,
            "回执编号": receipt_no,
            "关联交付": delivery_no,
            "接收凭据": receipt_token,
            "接收目标": delivery.get("接收目标", ""),
            "操作人": operator,
            "接收时间": now,
            "接收行": written_rows,
            "重复行": skipped,
            "接收行数": str(sum(len(rows) for rows in written_rows.values())),
            "幂等重放": False,
        }
        store.rows("publish_receipt").append(receipt)

        delivery["已接收明细"] = received
        delivery["已接收行数"] = str(sum(len(rows) for rows in received.values()))
        delivery["剩余选择"] = new_remaining
        delivery["接收回执"] = join_csv(
            [row.get("回执编号", "") for row in self._receipts(delivery_no)])
        delivery["最后接收时间"] = now
        if fully and not interrupted:
            delivery["status"] = DELIVERY_RECEIVED
            delivery["pending"] = False
            delivery["abnormal"] = False
        else:
            delivery["status"] = DELIVERY_INTERRUPTED
            delivery["pending"] = True
            delivery["abnormal"] = True

        self._sync_shards(delivery)

        ledger = self._upsert_ledger(delivery)
        self._refresh_ledger(ledger, delivery)

        if fully and not interrupted:
            message = (
                f"交付 {delivery_no} 接收完成：{delivery['已接收行数']} 行，"
                f"回执 {receipt_no}")
        else:
            missing = self.manager.remaining_rows(new_remaining)
            message = (
                f"交付 {delivery_no} 接收中断：本次写入 "
                f"{receipt['接收行数']} 行，仍缺 {missing} 行，"
                f"回执 {receipt_no} 可继续补做")
        if skipped:
            message += f"；目标已存在的行跳过 {sum(len(v) for v in skipped.values())} 行"
        return True, message, self.delivery_detail(delivery_no)

    def _target_rows(self, delivery: dict[str, Any]) -> dict[str, set[str]]:
        target = str(delivery.get("接收目标") or "")
        rows: dict[str, set[str]] = {}
        for row in store.rows("publish_target_row"):
            if row.get("接收目标") != target:
                continue
            rows.setdefault(str(row.get("分片编号")), set()).add(
                str(row.get("行标识")))
        return rows

    def _sync_shards(self, delivery: dict[str, Any]) -> None:
        received = dict(delivery.get("已接收明细") or {})
        for item in delivery.get("冻结范围", []):
            shard_no = str(item.get("分片编号"))
            total = len(item.get("行标识") or [])
            got = len(set(received.get(shard_no, [])))
            if got >= total and total > 0:
                self.shards.mark_progress(shard_no, "已送达")
            elif got > 0:
                self.shards.mark_progress(shard_no, "待补收")
            else:
                self.shards.mark_progress(shard_no, "已领取")

    def _refresh_ledger(self, ledger: dict[str, Any],
                        delivery: dict[str, Any]) -> None:
        if ledger.get("status") == "已送达" and delivery.get("status") != DELIVERY_RECEIVED:
            return
        criteria = dict(delivery.get("判定口径") or {})
        ledger["已接收行数"] = delivery.get("已接收行数", "0")
        ledger["接收回执"] = delivery.get("接收回执", "")
        counts = []
        for item in delivery.get("冻结范围", []):
            shard_no = str(item.get("分片编号"))
            got = len(set(
                dict(delivery.get("已接收明细") or {}).get(shard_no, [])))
            counts.append(f"{shard_no}:{got}/{len(item.get('行标识') or [])}")
        ledger["接收分片行数"] = ",".join(counts)
        ledger["投递时间"] = delivery.get("最后接收时间", "")
        ledger["重试次数"] = str(max(0, len(self._receipts(
            str(delivery["交付编号"]))) - 1))
        if delivery.get("status") == DELIVERY_RECEIVED:
            ledger["status"] = "已送达"
            ledger["pending"] = False
            ledger["abnormal"] = False
            ledger["投递结论"] = (
                f"接收完成：分片 {delivery.get('分片范围')} 共 "
                f"{delivery.get('已接收行数')} 行；回执 "
                f"{ledger.get('接收回执')}")
        elif delivery.get("status") == DELIVERY_INTERRUPTED:
            ledger["status"] = "部分送达"
            ledger["pending"] = True
            ledger["abnormal"] = True
            missing = self.manager.remaining_rows(
                dict(delivery.get("剩余选择") or {}))
            ledger["投递结论"] = (
                f"部分接收：已收 {delivery.get('已接收行数')} 行，"
                f"缺 {missing} 行待补做；回执 {ledger.get('接收回执')}")
        else:
            ledger["投递结论"] = (
                f"已领取分片 {delivery.get('分片范围')}，"
                f"待接收至 {delivery.get('接收目标')}")
        ledger["已验证字段范围"] = join_csv(
            list(criteria.get("输出列集合") or []))
        ledger["分片身份"] = delivery.get("分片范围", "")

    # ------------------------------------------------------------------
    # 详情、继续处理与核对
    # ------------------------------------------------------------------
    def delivery_detail(self, delivery_no: str) -> dict[str, Any] | None:
        delivery = self._delivery(delivery_no)
        if delivery is None:
            return None
        receipts = [
            {
                key: row.get(key)
                for key in ("回执编号", "接收凭据", "接收时间", "接收行数",
                            "status", "接收行", "重复行", "幂等重放")
            }
            for row in self._receipts(delivery_no)
        ]
        summary = self.manager.shard_received_counts(
            list(delivery.get("冻结范围") or []),
            dict(delivery.get("已接收明细") or {}))
        for line in summary:
            shard = self.shards.find(str(line["分片编号"]))
            line["所属交付"] = (
                str(shard.get("所属交付") or "") if shard else delivery_no)
            line["所属确认单"] = (
                str(shard.get("所属确认单") or "") if shard else "")
        remaining = dict(delivery.get("剩余选择") or {})
        delivered_shards = [item["分片编号"] for item in summary
                            if item["是否收齐"]]
        pending_shards = [item["分片编号"] for item in summary
                          if not item["是否收齐"]]
        detail = dict(delivery)
        criteria = dict(delivery.get("判定口径") or {})
        contract = self._contract(str(delivery.get("关联契约") or ""))
        current = (self.manager.criteria_snapshot(contract, _now())
                   if contract else criteria)
        conclusion = self.manager.conclude(
            criteria, current, list(delivery.get("冻结范围") or []))
        detail.update({
            "分片回执": summary,
            "回执明细": receipts,
            "剩余行数": self.manager.remaining_rows(remaining),
            "已收齐分片": delivered_shards,
            "未收齐分片": pending_shards,
            "可继续处理": bool(remaining) and delivery.get("status") != DELIVERY_RECEIVED,
            "可核对": bool(delivered_shards),
            "口径回查": conclusion,
            "投递台账": dict(self._ledger(delivery_no) or {}),
        })
        return detail

    def continue_delivery(self, delivery_no: str) -> tuple[
        bool, str, dict[str, Any] | None]:
        """继续处理入口：只对未送达的分片沿用可恢复的剩余选择。"""
        detail = self.delivery_detail(delivery_no)
        if detail is None:
            return False, f"交付 {delivery_no} 不存在", None
        if detail["status"] == DELIVERY_RECEIVED:
            return False, (
                f"交付 {delivery_no} 已送达，只接受核对，不能继续投递"), detail
        if not detail["剩余选择"]:
            return False, f"交付 {delivery_no} 没有可继续处理的行", detail
        selection = ", ".join(
            f"{shard}:{len(rows)}行"
            for shard, rows in detail["剩余选择"].items())
        return True, (
            f"交付 {delivery_no} 沿用剩余选择继续处理（{selection}）"), detail

    def verify_delivery(self, delivery_no: str,
                        now: str | None = None) -> tuple[
                            bool, str, dict[str, Any] | None]:
        """核对已送达的交付：只读回执，不再写目标位置。"""
        detail = self.delivery_detail(delivery_no)
        if detail is None:
            return False, f"交付 {delivery_no} 不存在", None
        if detail["status"] != DELIVERY_RECEIVED:
            return False, (
                f"交付 {delivery_no} 仍有未送达分片，只能继续处理，不能核对"), detail
        delivery = self._delivery(delivery_no)
        assert delivery is not None
        delivery["核对时间"] = now or _now()
        for line in detail["分片回执"]:
            self.shards.mark_progress(str(line["分片编号"]), "已核对")
        ledger = self._ledger(delivery_no)
        if ledger is not None:
            ledger["核对时间"] = delivery["核对时间"]
        detail = self.delivery_detail(delivery_no)
        return True, (
            f"交付 {delivery_no} 核对完成：分片 {detail['分片范围']} "
            f"共 {detail['已接收行数']} 行"), detail

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_sheets(self, contract_no: str = "") -> list[dict[str, Any]]:
        return [
            row for row in store.rows("confirm_sheet")
            if not contract_no or row.get("关联契约") == contract_no
        ]

    def sheet_detail(self, sheet_no: str) -> dict[str, Any] | None:
        sheet = self._sheet(sheet_no)
        if sheet is None:
            return None
        detail = dict(sheet)
        lines = list(sheet.get("行明细") or [])
        detail["可发分片列表"] = [l["分片编号"] for l in lines if l.get("可发")]
        detail["缺列分片列表"] = [l["分片编号"] for l in lines
                                 if l.get("缺失必填列")]
        detail["交付列表"] = [
            {"交付编号": d["交付编号"], "status": d["status"],
             "分片范围": d.get("分片范围"), "已接收行数": d.get("已接收行数")}
            for d in self.list_deliveries(contract_no=str(sheet.get("关联契约")))
            if d.get("关联确认单") == sheet_no
        ]
        return detail

    def list_deliveries(self, contract_no: str = "",
                        status: str = "") -> list[dict[str, Any]]:
        return [
            row for row in store.rows("publish_delivery")
            if (not contract_no or row.get("关联契约") == contract_no)
            and (not status or row.get("status") == status)
        ]

    def shard_overview(self, contract_no: str = "") -> list[dict[str, Any]]:
        rows = self.shards.by_contract(contract_no) if contract_no \
            else store.rows("publish_shard")
        return [dict(row) for row in rows]

    def target_rows(self, target: str = "") -> list[dict[str, Any]]:
        return [
            dict(row) for row in store.rows("publish_target_row")
            if not target or row.get("接收目标") == target
        ]


def _now() -> str:
    from datetime import datetime
    return datetime.now().replace(microsecond=0).isoformat()


service = ShardPublishService()
