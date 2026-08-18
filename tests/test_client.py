"""Client transport tests, all against the fake editor - no Unreal needed."""

from __future__ import annotations

import asyncio
import json

import pytest

from hullforge_mcp.client import (
    BridgeClient,
    BridgeError,
    ToolFailed,
    ToolOutcomeUnknown,
)
from hullforge_mcp.framing import encode_frame
from hullforge_mcp.session import SessionInfo

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def connected(editor) -> BridgeClient:
    c = BridgeClient(editor.session())
    await c.connect()
    return c


class TestHandshake:
    async def test_connects_and_records_server_version(self, editor):
        c = await connected(editor)
        assert c.connected
        assert c.server_version == "0.1.0"
        await c.close()

    async def test_wrong_token_is_rejected(self, editor):
        bad = SessionInfo(port=editor.port, token="wrong", pid=0)
        c = BridgeClient(bad)
        with pytest.raises(BridgeError, match="Handshake rejected"):
            await c.connect()
        await c.close()

    async def test_connect_to_dead_port_explains_stale_session(self):
        # Port 1 on loopback: nothing listens there.
        c = BridgeClient(SessionInfo(port=1, token="x", pid=0))
        with pytest.raises(BridgeError, match="stale"):
            await c.connect(timeout=2.0)


class TestCall:
    async def test_round_trip_returns_result_object(self, editor):
        @editor.tool("hf_ping")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok",
                    "result": {"server": "hullforge", "tool_count": 3}}

        c = await connected(editor)
        result = await c.call("hf_ping")
        assert result["server"] == "hullforge"
        assert result["tool_count"] == 3
        await c.close()

    async def test_args_are_forwarded(self, editor):
        @editor.tool("echo")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok",
                    "result": {"got": msg.get("args")}}

        c = await connected(editor)
        result = await c.call("echo", {"value": "round-trip", "n": 7})
        assert result["got"] == {"value": "round-trip", "n": 7}
        await c.close()

    async def test_absent_args_send_an_empty_object(self, editor):
        @editor.tool("echo")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok",
                    "result": {"got": msg.get("args")}}

        c = await connected(editor)
        assert (await c.call("echo"))["got"] == {}
        await c.close()

    async def test_ids_increment_and_do_not_repeat(self, editor):
        @editor.tool("t")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {}}

        c = await connected(editor)
        for _ in range(5):
            await c.call("t")
        ids = [m["id"] for m in editor.received]
        assert ids == sorted(ids) and len(set(ids)) == 5
        await c.close()

    async def test_missing_result_object_yields_empty_dict(self, editor):
        @editor.tool("t")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok"}

        c = await connected(editor)
        assert await c.call("t") == {}
        await c.close()


class TestFailureSemantics:
    async def test_failed_status_raises_ToolFailed(self, editor):
        @editor.tool("boom")
        def _(msg):
            return {"id": msg["id"], "ok": False, "status": "failed",
                    "error": "asset not found"}

        c = await connected(editor)
        with pytest.raises(ToolFailed, match="asset not found") as exc:
            await c.call("boom")
        assert exc.value.tool == "boom"
        await c.close()

    async def test_unknown_status_raises_a_DIFFERENT_exception(self, editor):
        # This distinction is the point of the whole protocol: "failed" is safe
        # to retry, "unknown" is not, and the type system should enforce that a
        # caller cannot treat them the same by accident.
        @editor.tool("slow")
        def _(msg):
            return {"id": msg["id"], "ok": False, "status": "unknown",
                    "error": "Timed out. OUTCOME UNKNOWN."}

        c = await connected(editor)
        with pytest.raises(ToolOutcomeUnknown):
            await c.call("slow")
        await c.close()

    async def test_unknown_is_not_a_subclass_of_failed(self):
        assert not issubclass(ToolOutcomeUnknown, ToolFailed)
        assert not issubclass(ToolFailed, ToolOutcomeUnknown)

    async def test_unknown_tool_surfaces_as_ToolFailed(self, editor):
        c = await connected(editor)
        with pytest.raises(ToolFailed, match="Unknown tool"):
            await c.call("nope")
        await c.close()


class TestProtocolFaults:
    async def test_id_mismatch_is_detected(self, editor):
        @editor.tool("t")
        def _(msg):
            return {"id": 9999, "ok": True, "status": "ok", "result": {}}

        c = await connected(editor)
        with pytest.raises(BridgeError, match="correlation mismatch"):
            await c.call("t")
        await c.close()

    async def test_malformed_json_from_editor(self, editor):
        editor.raw_override = encode_frame("this is not json")
        c = await connected(editor)
        with pytest.raises(BridgeError, match="malformed JSON"):
            await c.call("t")
        await c.close()

    async def test_oversized_frame_from_editor_is_a_framing_fault(self, editor):
        editor.raw_override = b"\xff\xff\xff\x7f"
        c = await connected(editor)
        with pytest.raises(BridgeError, match="Framing fault"):
            await c.call("t")
        await c.close()

    async def test_editor_disconnect_mid_session(self, editor):
        editor.drop_after_handshake = True
        c = BridgeClient(editor.session())
        await c.connect()
        with pytest.raises(BridgeError, match="closed the connection"):
            await c.call("t")
        await c.close()

    async def test_request_timeout_warns_against_blind_retry(self, editor):
        # A tool the editor never answers - the exact shape of the editor being
        # wedged behind a modal dialog. The message must not read as "failed",
        # because the operation may still be in flight.
        editor.silent_tools.add("hang")

        c = BridgeClient(editor.session(), request_timeout=0.5)
        await c.connect()
        with pytest.raises(BridgeError, match="verify before retrying"):
            await c.call("hang")
        await c.close()


class TestConcurrency:
    async def test_parallel_calls_are_serialised_and_correlated(self, editor):
        @editor.tool("t")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok",
                    "result": {"echo": msg["args"].get("n")}}

        c = await connected(editor)
        results = await asyncio.gather(*(c.call("t", {"n": i}) for i in range(20)))
        assert [r["echo"] for r in results] == list(range(20))
        await c.close()
