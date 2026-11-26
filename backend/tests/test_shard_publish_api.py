"""待发布分片接口测试：走真实 ASGI 应用，确认路由优先级与响应契约。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.main import app
from app.store import store


@pytest.fixture(autouse=True)
def fresh_store():
    store.reload_seed()
    yield store
    store.reload_seed()


def call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """最小 ASGI 调用器：环境未安装 httpx，直接跑应用拿回响应体。"""
    raw = json.dumps(body).encode() if body is not None else b""
    route, _, query = path.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": route,
        "raw_path": route.encode(), "query_string": query.encode(),
        "root_path": "", "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages
                       if m["type"] == "http.response.body")
    return status, json.loads(payload or b"null")


class TestShardRoutes:
    def test_receipt_not_shadowed_by_module_route(self):
        status, payload = call("GET", "/api/shard-publish/receipt?contract=OC-S01")
        assert status == 200
        assert payload["ok"] is True
        assert payload["entry"]["关联契约"] == "OC-S01"

    def test_generic_module_route_still_works(self):
        status, payload = call("GET", "/api/output_contract/1")
        assert status == 200
        assert payload["契约编号"] == "OC-001"

    def test_internal_tables_not_exposed_as_modules(self):
        status, _payload = call("GET", "/api/publish_shard")
        assert status == 404

    def test_full_http_journey(self):
        _s, sheet = call("POST", "/api/shard-publish/sheets", {
            "contract_no": "OC-S01",
            "target": "/data/cooked/publish/settled/",
            "shard_numbers": ["SH-A01", "SH-B01"],
            "operator": "publisher-01",
        })
        assert sheet["ok"] is True
        sheet_no = sheet["entry"]["确认单编号"]
        assert sheet["entry"]["可发分片"] == "SH-A01"
        assert sheet["entry"]["缺列分片"] == "SH-B01"

        _s, submitted = call(
            "POST", f"/api/shard-publish/sheets/{sheet_no}/submit",
            {"operator": "publisher-01"})
        assert submitted["ok"] is True
        delivery_no = submitted["entry"]["交付编号"]
        assert submitted["entry"]["分片范围"] == "SH-A01"

        _s, supplemented = call("POST", "/api/shard-publish/supplement", {
            "shard_no": "SH-B01", "add_columns": ["currency"]})
        assert supplemented["ok"] is True

        _s, first = call(
            "POST", f"/api/shard-publish/deliveries/{delivery_no}/receive",
            {"receipt_token": "TK-1", "written": {"SH-A01": ["A-R1"]},
             "interrupted": True})
        assert first["ok"] is True
        assert first["entry"]["status"] == "已中断"
        assert first["entry"]["剩余选择"] == {"SH-A01": ["A-R2"]}

        _s, replay = call(
            "POST", f"/api/shard-publish/deliveries/{delivery_no}/receive",
            {"receipt_token": "TK-1", "written": {"SH-A01": ["A-R1"]}})
        assert replay["ok"] is True
        assert "不重复写入" in replay["message"]

        _s, done = call(
            "POST", f"/api/shard-publish/deliveries/{delivery_no}/receive",
            {"receipt_token": "TK-2", "written": {"SH-A01": ["A-R2"]}})
        assert done["entry"]["status"] == "已接收"

        _s, verified = call(
            "POST", f"/api/shard-publish/deliveries/{delivery_no}/verify")
        assert verified["ok"] is True

        _s, rows = call("GET", "/api/shard-publish/target-rows")
        assert [(r["分片编号"], r["行标识"]) for r in rows["items"]] == [
            ("SH-A01", "A-R1"), ("SH-A01", "A-R2")]

        _s, ledger = call(
            "GET", f"/api/shard-publish/deliveries/{delivery_no}")
        assert ledger["投递台账"]["已验证字段范围"] == "id,currency,amount"
        assert ledger["投递台账"]["接收分片行数"] == "SH-A01:2/2"

    def test_generic_action_cannot_rewrite_shard_ledger(self):
        _s, sheet = call("POST", "/api/shard-publish/sheets", {
            "contract_no": "OC-S01",
            "target": "/data/cooked/publish/settled/",
            "shard_numbers": ["SH-A01"], "operator": "publisher-01"})
        _s, submitted = call(
            "POST", f"/api/shard-publish/sheets/{sheet['entry']['确认单编号']}/submit",
            {"operator": "publisher-01"})
        delivery_no = submitted["entry"]["交付编号"]
        ledger_id = submitted["entry"]["投递台账"]["id"]
        status, result = call(
            "POST", f"/api/result_deliver/{ledger_id}/actions",
            {"action": "确认送达"})
        assert status == 200
        assert result["ok"] is False
        assert delivery_no in result["message"]
