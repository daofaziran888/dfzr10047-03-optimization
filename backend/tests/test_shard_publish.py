"""待发布分片发布链路测试：逐片校验、领取冻结、回执幂等与旧交付回查。"""
from __future__ import annotations

import pytest

from app.pipeline.shard_publish_manager import ShardPublishManager
from app.services.shard_publish import ShardPublishService, service
from app.store import store

TARGET = "/data/cooked/publish/settled/"


@pytest.fixture(autouse=False)
def fresh_store():
    store.reload_seed()
    yield store
    store.reload_seed()


def shard(no: str) -> dict:
    return next(r for r in store.rows("publish_shard") if r["分片编号"] == no)


def target_rows(no: str | None = None) -> list[tuple[str, str]]:
    return [
        (r["分片编号"], r["行标识"])
        for r in store.rows("publish_target_row")
        if no is None or r["分片编号"] == no
    ]


class TestShardValidation:
    def test_each_shard_checked_by_own_required_columns(self, fresh_store):
        """A、B 都含 id，但 B 缺必填币种：A 可单独交付，B 判缺列。"""
        svc = ShardPublishService()
        ok, _msg, payload = svc.shard_receipt_by_target("OC-S01", TARGET)
        assert ok is True
        target = payload["接收目标"][0]
        assert target["可发分片"] == ["SH-A01"]
        assert target["缺列分片"] == ["SH-B01"]
        lines = {line["分片编号"]: line for line in target["分片"]}
        assert lines["SH-A01"]["可发"] is True
        assert lines["SH-B01"]["缺失必填列"] == ["currency"]
        # 整批列并集里有 currency，但 B 自身缺列，不能被并集掩盖
        assert "currency" in payload["判定口径"]["输出列集合"]

    def test_receipt_grouped_by_receiver_target(self, fresh_store):
        svc = ShardPublishService()
        ok, _msg, payload = svc.shard_receipt_by_target("OC-S01")
        assert ok is True
        targets = {t["接收目标"]: t for t in payload["接收目标"]}
        assert targets[TARGET]["可发分片"] == ["SH-A01"]
        assert targets["/data/cooked/publish/backup/"]["可发分片"] == ["SH-C01"]

    def test_unapproved_contract_cannot_produce_receipt(self, fresh_store):
        svc = ShardPublishService()
        ok, msg, _payload = svc.shard_receipt_by_target("OC-002")
        assert ok is False
        assert "未通过" in msg

    def test_extra_column_outside_contract_blocks_shard(self, fresh_store):
        svc = ShardPublishService()
        svc.shards.supplement("SH-A01", add_columns=["ghost"])
        ok, _msg, payload = svc.shard_receipt_by_target("OC-S01", TARGET)
        assert ok is True
        line = payload["接收目标"][0]["分片"][0]
        assert line["可发"] is False
        assert line["未约定列"] == ["ghost"]

    def test_union_of_batch_columns_does_not_satisfy_shard(self, fresh_store):
        """A、B 的整批列并集含 currency，但 B 自身缺 currency 仍判缺列。"""
        svc = ShardPublishService()
        all_columns = {column for shard_no in ("SH-A01", "SH-B01")
                       for column in svc.shards.find(shard_no)["分片列"].split(",")}
        assert "currency" in all_columns
        ok, _msg, payload = svc.shard_receipt_by_target("OC-S01", TARGET)
        assert ok is True
        lines = {l["分片编号"]: l for l in payload["接收目标"][0]["分片"]}
        assert lines["SH-A01"]["可发"] is True
        assert lines["SH-B01"]["可发"] is False
        assert lines["SH-B01"]["缺失必填列"] == ["currency"]

    def test_shard_level_mandatory_override(self, fresh_store):
        """分片可声明更严的必填列，服务端按分片自身口径判定。"""
        svc = ShardPublishService()
        ok, _msg = svc.shards.supplement(
            "SH-A01", required_columns=["id", "currency", "amount"])
        assert ok is True
        ok, _msg, payload = svc.shard_receipt_by_target("OC-S01", TARGET)
        line = payload["接收目标"][0]["分片"][0]
        assert line["可发"] is True
        svc.shards.supplement(
            "SH-A01", required_columns=["id", "currency", "channel"])
        ok, _msg, payload = svc.shard_receipt_by_target("OC-S01", TARGET)
        line = payload["接收目标"][0]["分片"][0]
        assert line["可发"] is False
        assert line["缺失必填列"] == ["channel"]


class TestConfirmSheetAndClaim:
    def _sheet(self, svc=None, shards=("SH-A01", "SH-B01")):
        svc = svc or ShardPublishService()
        ok, msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, list(shards), "publisher-01")
        assert ok is True, msg
        return svc, sheet

    def test_sheet_shows_publishable_and_missing_on_one_page(self, fresh_store):
        _svc, sheet = self._sheet()
        assert sheet["可发分片"] == "SH-A01"
        assert sheet["缺列分片"] == "SH-B01"
        detail = service.sheet_detail(sheet["确认单编号"])
        assert detail["可发分片列表"] == ["SH-A01"]
        assert detail["缺列分片列表"] == ["SH-B01"]
        decisions = {l["分片编号"]: l["判定结论"] for l in detail["行明细"]}
        assert decisions == {"SH-A01": "可发", "SH-B01": "缺列"}

    def test_submit_claims_only_qualified_shard(self, fresh_store):
        svc, sheet = self._sheet()
        ok, msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        assert ok is True, msg
        assert detail["分片范围"] == "SH-A01"
        assert detail["总行数"] == "2"
        assert "未并入本次交付：SH-B01" in msg
        assert shard("SH-A01")["status"] == "已领取"
        assert shard("SH-A01")["所属交付"] == detail["交付编号"]
        assert shard("SH-B01")["status"] == "待发布"

    def test_submit_revalidates_actual_fields(self, fresh_store):
        """确认单编制后 B 在服务端被补上币种，提交时应按实际字段重新判定。"""
        svc, sheet = self._sheet()
        svc.shards.supplement("SH-B01", add_columns=["currency"])
        ok, msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        assert ok is True, msg
        assert detail["分片范围"] == "SH-A01,SH-B01"
        assert detail["总行数"] == "4"
        assert shard("SH-B01")["status"] == "已领取"

    def test_supplement_after_claim_does_not_alter_old_delivery(self, fresh_store):
        """B 的补值在 A 被领取后才到：不得并入旧交付，只能另开交付。"""
        svc, sheet = self._sheet()
        _ok, _msg, first = svc.submit_confirm_sheet(sheet["确认单编号"])
        ok, msg = svc.shards.supplement("SH-B01", add_columns=["currency"])
        assert ok is True

        # 旧交付的分片范围与冻结行不变
        again = svc.delivery_detail(first["交付编号"])
        assert again["分片范围"] == "SH-A01"
        assert again["总行数"] == "2"
        old_scope = {item["分片编号"]: item["行标识"] for item in again["冻结范围"]}
        assert old_scope == {"SH-A01": ["A-R1", "A-R2"]}
        # 已领取的分片本身不能再被补值改写
        ok2, msg2 = svc.shards.supplement("SH-A01", add_columns=["ghost"])
        assert ok2 is False
        assert "已被交付领取" in msg2

        # B 只能新开确认单另行交付
        ok3, msg3, second_sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-B01"], "publisher-01")
        assert ok3 is True, msg3
        ok4, _m4, second = svc.submit_confirm_sheet(second_sheet["确认单编号"])
        assert ok4 is True
        assert second["分片范围"] == "SH-B01"
        assert second["交付编号"] != first["交付编号"]
        assert shard("SH-B01")["所属交付"] == second["交付编号"]

    def test_submit_with_all_shards_unqualified_is_refused(self, fresh_store):
        svc, sheet = self._sheet(shards=("SH-B01",))
        ok, msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        assert ok is False
        assert "均不可发布" in msg
        assert "缺失必填列" in msg
        assert detail is None

    def test_submitted_sheet_cannot_be_claimed_twice(self, fresh_store):
        svc, sheet = self._sheet()
        svc.submit_confirm_sheet(sheet["确认单编号"])
        ok, msg, _detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        assert ok is False
        assert "已提交" in msg


class TestFrozenCriteria:
    def test_delivery_records_scope_and_criteria(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(
            sheet["确认单编号"], now="2026-09-21T10:00:00")
        assert detail["判定口径"]["必填列"] == ["id", "currency"]
        assert detail["判定口径"]["快照时间"] == "2026-09-21T10:00:00"
        assert detail["冻结判定"][0]["分片编号"] == "SH-A01"
        assert detail["冻结判定"][0]["必填列"] == ["id", "currency"]

    def test_later_threshold_change_does_not_rewrite_old_conclusion(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        delivery_no = detail["交付编号"]

        # 契约事后把必填列收紧到包含 amount，并把分片行数下限提到 3
        contract = next(r for r in store.rows("output_contract")
                        if r["契约编号"] == "OC-S01")
        contract["必填列"] = "id,currency,amount"
        contract["分片行数下限"] = "3"

        reviewed = svc.delivery_detail(delivery_no)
        assert reviewed["判定口径"]["必填列"] == ["id", "currency"]
        assert reviewed["判定口径"]["分片行数下限"] == 1
        assert reviewed["冻结判定"][0]["结论"].startswith("可发布")
        assert reviewed["口径回查"]["沿用冻结口径"] is True
        assert set(reviewed["口径回查"]["口径变更项"]) == {"必填列", "分片行数下限"}

    def test_new_submission_uses_current_criteria(self, fresh_store):
        svc = ShardPublishService()
        contract = next(r for r in store.rows("output_contract")
                        if r["契约编号"] == "OC-S01")
        contract["必填列"] = "id,currency,amount"
        ok, msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        assert ok is True, msg
        assert sheet["可发分片"] == "SH-A01"
        ok, msg, sheet_b = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-B01"], "publisher-01")
        assert sheet_b["缺列分片"] == "SH-B01"


class TestReceiptAndIdempotency:
    def _claim(self, svc=None):
        svc = svc or ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        return svc, detail["交付编号"]

    def test_interrupted_receipt_keeps_remaining_selection(self, fresh_store):
        svc, delivery_no = self._claim()
        ok, msg, detail = svc.receive(
            delivery_no, "TK-1", written={"SH-A01": ["A-R1"]}, interrupted=True)
        assert ok is True, msg
        assert detail["status"] == "已中断"
        assert detail["剩余选择"] == {"SH-A01": ["A-R2"]}
        assert detail["可继续处理"] is True

    def test_replay_same_token_counts_once(self, fresh_store):
        svc, delivery_no = self._claim()
        svc.receive(delivery_no, "TK-1", written={"SH-A01": ["A-R1"]},
                    interrupted=True)
        before = target_rows()
        ok, msg, detail = svc.receive(
            delivery_no, "TK-1", written={"SH-A01": ["A-R1"]})
        assert ok is True
        assert "不重复写入" in msg
        assert target_rows() == before
        assert len(detail["回执明细"]) == 1

    def test_same_token_cannot_settle_another_delivery(self, fresh_store):
        svc, first_no = self._claim()
        svc.receive(first_no, "TK-SHARED", written={"SH-A01": ["A-R1"]},
                    interrupted=True)
        _ok, _msg, sheet_b = svc.build_confirm_sheet(
            "OC-S01", "/data/cooked/publish/backup/", ["SH-C01"], "publisher-01")
        _ok, _msg, second = svc.submit_confirm_sheet(sheet_b["确认单编号"])
        ok, msg, _detail = svc.receive(
            second["交付编号"], "TK-SHARED", written={"SH-C01": ["C-R1"]})
        assert ok is False
        assert "不能跨交付重复结算" in msg

    def test_target_does_not_gain_duplicate_rows(self, fresh_store):
        svc, delivery_no = self._claim()
        svc.receive(delivery_no, "TK-1", written={"SH-A01": ["A-R1"]},
                    interrupted=True)
        svc.continue_delivery(delivery_no)
        svc.receive(delivery_no, "TK-2", written={"SH-A01": ["A-R1", "A-R2"]})
        rows = target_rows()
        assert sorted(rows) == [("SH-A01", "A-R1"), ("SH-A01", "A-R2")]
        assert len(rows) == len(set(rows))

    def test_requested_row_outside_remaining_refused(self, fresh_store):
        svc, delivery_no = self._claim()
        svc.receive(delivery_no, "TK-1", written={"SH-A01": ["A-R1"]},
                    interrupted=True)
        ok, msg, _detail = svc.receive(
            delivery_no, "TK-2", written={"SH-A01": ["A-R9"]})
        assert ok is False
        assert "不在剩余选择内" in msg

    def test_receive_completes_and_closes(self, fresh_store):
        svc, delivery_no = self._claim()
        svc.receive(delivery_no, "TK-1", written={"SH-A01": ["A-R1"]},
                    interrupted=True)
        ok, msg, detail = svc.receive(
            delivery_no, "TK-2", written={"SH-A01": ["A-R2"]})
        assert ok is True, msg
        assert detail["status"] == "已接收"
        assert detail["剩余行数"] == 0
        assert detail["已收齐分片"] == ["SH-A01"]
        assert detail["分片回执"][0]["已接收行数"] == 2
        assert shard("SH-A01")["status"] == "已送达"


class TestContinueAndVerify:
    def _delivered(self, svc=None):
        svc = svc or ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        delivery_no = detail["交付编号"]
        svc.receive(delivery_no, "TK-1", written={"SH-A01": ["A-R1"]},
                    interrupted=True)
        return svc, delivery_no

    def test_undelivered_offers_continue_entry(self, fresh_store):
        svc, delivery_no = self._delivered()
        ok, msg, detail = svc.continue_delivery(delivery_no)
        assert ok is True
        assert "剩余选择" in msg
        assert detail["剩余选择"] == {"SH-A01": ["A-R2"]}

    def test_delivered_only_accepts_verify(self, fresh_store):
        svc, delivery_no = self._delivered()
        svc.receive(delivery_no, "TK-2", written={"SH-A01": ["A-R2"]})
        ok, msg, detail = svc.continue_delivery(delivery_no)
        assert ok is False
        assert "只接受核对" in msg
        assert detail["可继续处理"] is False
        ok2, msg2, verified = svc.verify_delivery(delivery_no)
        assert ok2 is True
        assert verified["投递台账"]["status"] == "已送达"

    def test_verify_refused_while_shards_pending(self, fresh_store):
        svc, delivery_no = self._delivered()
        ok, msg, _detail = svc.verify_delivery(delivery_no)
        assert ok is False
        assert "未送达分片" in msg

    def test_verify_writes_no_target_rows(self, fresh_store):
        svc, delivery_no = self._delivered()
        svc.receive(delivery_no, "TK-2", written={"SH-A01": ["A-R2"]})
        before = target_rows()
        svc.verify_delivery(delivery_no)
        assert target_rows() == before


class TestLedgerLinkage:
    def test_ledger_reads_contract_field_scope(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        ledger = detail["投递台账"]
        assert ledger["投递记录编号"] == f"RD-{detail['交付编号']}"
        assert ledger["已验证字段范围"] == "id,currency,amount"
        assert ledger["分片身份"] == "SH-A01"
        assert ledger["关联契约"] == "OC-S01"

    def test_ledger_records_received_identity_and_rows(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        delivery_no = detail["交付编号"]
        svc.receive(delivery_no, "TK-1", written={"SH-A01": ["A-R1"]},
                    interrupted=True)
        svc.receive(delivery_no, "TK-2", written={"SH-A01": ["A-R2"]})
        ledger = svc.delivery_detail(delivery_no)["投递台账"]
        assert ledger["status"] == "已送达"
        assert ledger["接收分片行数"] == "SH-A01:2/2"
        assert "RC-0001" in ledger["接收回执"]
        assert ledger["已接收行数"] == "2"
        assert ledger["投递行数"] == "2"


class TestAttribution:
    def test_belongs_to_second_actual_delivery(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01", "SH-B01"], "publisher-01")
        _ok, _msg, first = svc.submit_confirm_sheet(sheet["确认单编号"])
        svc.shards.supplement("SH-B01", add_columns=["currency"])
        _ok, _msg, sheet_b = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-B01"], "publisher-01")
        _ok, _msg, second = svc.submit_confirm_sheet(sheet_b["确认单编号"])

        assert first["分片范围"] == "SH-A01"
        assert second["分片范围"] == "SH-B01"
        assert shard("SH-B01")["所属交付"] == second["交付编号"]
        assert shard("SH-B01")["所属确认单"] == sheet_b["确认单编号"]
        line = svc.delivery_detail(second["交付编号"])["分片回执"][0]
        assert line["所属交付"] == second["交付编号"]

    def test_b_target_mismatch_not_claimed(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-C01"], "publisher-01")
        assert sheet["可发分片"] == ""
        ok, msg, _detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        assert ok is False
        assert "接收目标" in msg or "必填列" in msg
        assert shard("SH-C01")["status"] == "待发布"


class TestRecoveryEdgeCases:
    def test_completed_delivery_rejects_new_receipt(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        delivery_no = detail["交付编号"]
        svc.receive(delivery_no, "TK-1", written={"SH-A01": ["A-R1", "A-R2"]})
        ok, msg, _d = svc.receive(
            delivery_no, "TK-9", written={"SH-A01": ["A-R1"]})
        assert ok is False
        assert "只接受核对" in msg

    def test_receive_without_written_consumes_all_remaining(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        ok, _msg, done = svc.receive(detail["交付编号"], "TK-ALL")
        assert ok is True
        assert done["status"] == "已接收"
        assert len(target_rows("SH-A01")) == 2

    def test_new_token_after_completion_cannot_duplicate(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        svc.receive(detail["交付编号"], "TK-1", written={"SH-A01": ["A-R1", "A-R2"]})
        before = target_rows()
        svc.receive(detail["交付编号"], "TK-2", written={"SH-A01": ["A-R1"]})
        assert target_rows() == before

    def test_continue_then_receive_uses_current_remaining(self, fresh_store):
        svc = ShardPublishService()
        _ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01"], "publisher-01")
        _ok, _msg, detail = svc.submit_confirm_sheet(sheet["确认单编号"])
        delivery_no = detail["交付编号"]
        svc.receive(delivery_no, "TK-1", written={"SH-A01": ["A-R1"]},
                    interrupted=True)
        _ok, _msg, resumed = svc.continue_delivery(delivery_no)
        assert resumed["剩余选择"] == {"SH-A01": ["A-R2"]}
        ok, _msg, done = svc.receive(
            delivery_no, "TK-2", written=resumed["剩余选择"])
        assert ok is True
        assert done["status"] == "已接收"


class TestFullPublisherJourney:
    def test_a_then_b_after_supplement_then_interrupted_receipt(self, fresh_store):
        """A、B 各两行：领取 A 后补 B；断开一次回执再补做；目标只多 A、B 各两行。"""
        svc = ShardPublishService()
        ok, _msg, sheet = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-A01", "SH-B01"], "publisher-01")
        assert ok is True
        _ok, _msg, first = svc.submit_confirm_sheet(sheet["确认单编号"])
        assert first["分片范围"] == "SH-A01"

        ok, msg = svc.shards.supplement("SH-B01", add_columns=["currency"])
        assert ok is True
        ok, _msg, sheet_b = svc.build_confirm_sheet(
            "OC-S01", TARGET, ["SH-B01"], "publisher-01")
        ok, _msg, second = svc.submit_confirm_sheet(sheet_b["确认单编号"])
        assert ok is True
        assert second["分片范围"] == "SH-B01"

        # 交付 A 时断一次回执
        ok, msg, broken = svc.receive(
            first["交付编号"], "TK-A1", written={"SH-A01": ["A-R1"]})
        assert ok is True
        assert broken["status"] == "已中断"

        # 打开投递详情补做剩余行
        ok, msg, reopened = svc.continue_delivery(first["交付编号"])
        assert ok is True
        assert "A-R2" in str(reopened["剩余选择"])
        svc.receive(first["交付编号"], "TK-A2", written={"SH-A01": ["A-R2"]})
        svc.receive(second["交付编号"], "TK-B1",
                    written={"SH-B01": ["B-R1", "B-R2"]})

        rows = target_rows()
        assert sorted(rows) == [
            ("SH-A01", "A-R1"), ("SH-A01", "A-R2"),
            ("SH-B01", "B-R1"), ("SH-B01", "B-R2"),
        ]
        # B 属于第二次实际交付，页面能说清归属
        assert shard("SH-B01")["所属交付"] == second["交付编号"]
        b_detail = svc.delivery_detail(second["交付编号"])
        assert b_detail["分片范围"] == "SH-B01"
        assert b_detail["已接收行数"] == "2"
