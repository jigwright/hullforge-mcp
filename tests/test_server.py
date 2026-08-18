"""MCP server tests.

Handlers are called directly rather than through a transport - the point is
catalog translation and error mapping, not the SDK's plumbing.
"""

from __future__ import annotations

import json

import mcp.types as types
import pytest

from hullforge_mcp.server import HullForgeServer

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def describe_reply(msg, tools):
    return {"id": msg["id"], "ok": True, "status": "ok",
            "result": {"tools": tools, "count": len(tools)}}


SCHEMA = {
    "type": "object",
    "properties": {"asset_path": {"type": "string"}},
    "required": ["asset_path"],
    "additionalProperties": False,
}


@pytest.fixture
async def wired(editor, tmp_path, monkeypatch):
    """A HullForgeServer pointed at the fake editor, bypassing session discovery."""
    from hullforge_mcp import server as server_mod
    from hullforge_mcp.session import SessionInfo

    info = SessionInfo(port=editor.port, token=editor.token, pid=0,
                       project_name="FakeProject")
    monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])

    srv = HullForgeServer(tmp_path)
    try:
        yield srv, editor
    finally:
        if srv._client is not None:
            await srv._client.close()


def catalog_names(result) -> list[str]:
    """Tool names excluding hf_bridge_status.

    That one is synthesised by the sidecar and always present, so including it
    would make every catalog assertion about plumbing rather than about what
    the editor actually advertises.
    """
    from hullforge_mcp.server import _BRIDGE_STATUS_TOOL
    return [t.name for t in result.tools if t.name != _BRIDGE_STATUS_TOOL.name]


class TestCatalog:
    async def test_translates_described_tools(self, wired):
        srv, editor = wired

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg, [
                {"name": "hf_ping", "description": "liveness",
                 "mutating": False, "input_schema": {"type": "object", "properties": {}}},
                {"name": "hf_create_material", "description": "make a material",
                 "mutating": True, "input_schema": SCHEMA},
            ])

        result = await srv._handle_list_tools(None, None)
        assert catalog_names(result) == ["hf_ping", "hf_create_material"]

        mat = next(t for t in result.tools if t.name == "hf_create_material")
        assert mat.description == "make a material"
        # Schema must arrive verbatim from C++, not be re-derived here.
        assert mat.input_schema == SCHEMA

    async def test_mutating_flag_becomes_annotations(self, wired):
        srv, editor = wired

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg, [
                {"name": "reader", "description": "", "mutating": False,
                 "input_schema": {"type": "object"}},
                {"name": "writer", "description": "", "mutating": True,
                 "input_schema": {"type": "object"}},
            ])

        tools = {t.name: t for t in (await srv._handle_list_tools(None, None)).tools}
        assert tools["reader"].annotations.read_only_hint is True
        assert tools["writer"].annotations.read_only_hint is False

        # destructive_hint means "can destroy work", NOT "writes". A tool that
        # creates an asset is a write and not a hazard; flagging every mutation
        # destructive makes the flag meaningless, and a reader who learns to
        # ignore it also ignores it on hf_delete_asset.
        assert tools["reader"].annotations.destructive_hint is False
        assert tools["writer"].annotations.destructive_hint is False

    async def test_titles_are_derived_for_every_tool(self, wired):
        """The connector directory requires a title on every tool.

        Derived from the name rather than hand-written, so adding a tool in C++
        cannot forget one.
        """
        srv, editor = wired

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg, [
                {"name": "hf_set_material_parameter", "description": "",
                 "mutating": True, "input_schema": {"type": "object"}},
                {"name": "hf_list_pcg_nodes", "description": "",
                 "mutating": False, "input_schema": {"type": "object"}},
            ])

        tools = {t.name: t for t in (await srv._handle_list_tools(None, None)).tools}
        assert all(t.title for t in tools.values()), "every tool needs a title"
        assert tools["hf_set_material_parameter"].title == "Set Material Parameter"
        # Acronyms must not be naively capitalised into "Pcg".
        assert tools["hf_list_pcg_nodes"].title == "List PCG Nodes"

    async def test_deletion_is_flagged_destructive(self, wired):
        srv, editor = wired

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg, [
                {"name": "hf_delete_asset", "description": "", "mutating": True,
                 "input_schema": {"type": "object"}},
                {"name": "hf_create_material_instance", "description": "",
                 "mutating": True, "input_schema": {"type": "object"}},
            ])

        tools = {t.name: t for t in (await srv._handle_list_tools(None, None)).tools}
        assert tools["hf_delete_asset"].annotations.destructive_hint is True
        assert tools["hf_create_material_instance"].annotations.destructive_hint is False

    async def test_internal_tools_are_hidden(self, wired):
        srv, editor = wired

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg, [
                {"name": "hf_describe_tools", "description": "meta",
                 "mutating": False, "input_schema": {"type": "object"}},
                {"name": "hf_ping", "description": "",
                 "mutating": False, "input_schema": {"type": "object"}},
            ])

        assert catalog_names(await srv._handle_list_tools(None, None)) == ["hf_ping"]

    async def test_tool_without_schema_is_dropped_not_shipped_untyped(self, wired):
        # An untyped tool forces the model to guess parameters. Better absent.
        srv, editor = wired

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg, [
                {"name": "broken", "description": "", "mutating": False,
                 "input_schema": {}},
                {"name": "fine", "description": "", "mutating": False,
                 "input_schema": {"type": "object"}},
            ])

        assert catalog_names(await srv._handle_list_tools(None, None)) == ["fine"]

    async def test_unreachable_editor_yields_empty_catalog_not_a_crash(self, tmp_path, monkeypatch):
        from hullforge_mcp import server as server_mod
        from hullforge_mcp.session import SessionError

        def boom(_dir):
            raise SessionError("no session file")

        monkeypatch.setattr(server_mod, "discover_all", boom)
        srv = HullForgeServer(tmp_path)
        result = await srv._handle_list_tools(None, None)
        # Only the always-available status tool, which EXPLAINS the absence
        # rather than leaving an empty list that looks like a broken install.
        assert catalog_names(result) == []
        assert any(t.name == "hf_bridge_status" for t in result.tools)

    async def test_catalog_is_not_cached_across_calls(self, wired):
        # HullForge.Reload can change the catalog at any moment, so a stale
        # cached list is worse than re-fetching.
        srv, editor = wired
        state = {"tools": [{"name": "a", "description": "", "mutating": False,
                            "input_schema": {"type": "object"}}]}

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg, state["tools"])

        assert catalog_names(await srv._handle_list_tools(None, None)) == ["a"]
        state["tools"].append({"name": "b", "description": "", "mutating": False,
                               "input_schema": {"type": "object"}})
        assert catalog_names(await srv._handle_list_tools(None, None)) == ["a", "b"]


class Params:
    def __init__(self, name, arguments=None):
        self.name = name
        self.arguments = arguments


class TestCallTool:
    async def test_success_returns_text_and_structured_content(self, wired):
        srv, editor = wired

        @editor.tool("hf_ping")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok",
                    "result": {"server": "hullforge", "tool_count": 3}}

        result = await srv._handle_call_tool(None, Params("hf_ping"))
        assert result.is_error in (False, None)
        assert result.structured_content == {"server": "hullforge", "tool_count": 3}
        assert json.loads(result.content[0].text)["tool_count"] == 3

    async def test_arguments_are_forwarded(self, wired):
        srv, editor = wired

        @editor.tool("t")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok",
                    "result": {"got": msg["args"]}}

        result = await srv._handle_call_tool(None, Params("t", {"asset_path": "/Game/X"}))
        assert result.structured_content["got"] == {"asset_path": "/Game/X"}

    async def test_tool_failure_is_an_mcp_error_result(self, wired):
        srv, editor = wired

        @editor.tool("boom")
        def _(msg):
            return {"id": msg["id"], "ok": False, "status": "failed",
                    "error": "asset not found"}

        result = await srv._handle_call_tool(None, Params("boom"))
        assert result.is_error is True
        assert "asset not found" in result.content[0].text

    async def test_unknown_outcome_is_flagged_loudly_and_differently(self, wired):
        # The model must be told not to retry. This wording is the last line of
        # defence against a double-applied mutation.
        srv, editor = wired

        @editor.tool("slow")
        def _(msg):
            return {"id": msg["id"], "ok": False, "status": "unknown",
                    "error": "Timed out after 120s."}

        result = await srv._handle_call_tool(None, Params("slow"))
        text = result.content[0].text
        assert result.is_error is True
        assert "OUTCOME UNKNOWN" in text
        assert "retrying blind" in text
        assert "NOT a reported failure" in text

    async def test_unknown_and_failed_produce_different_text(self, wired):
        srv, editor = wired

        @editor.tool("f")
        def _(msg):
            return {"id": msg["id"], "ok": False, "status": "failed", "error": "x"}

        @editor.tool("u")
        def _(msg):
            return {"id": msg["id"], "ok": False, "status": "unknown", "error": "x"}

        failed = (await srv._handle_call_tool(None, Params("f"))).content[0].text
        unknown = (await srv._handle_call_tool(None, Params("u"))).content[0].text
        assert failed != unknown
        assert "OUTCOME UNKNOWN" not in failed

    async def test_no_editor_explains_rather_than_raising(self, tmp_path, monkeypatch):
        from hullforge_mcp import server as server_mod

        # Patched rather than relying on there genuinely being no editor: the
        # registry is user-global, so a real editor open on this machine would
        # otherwise make this test pass or fail by accident.
        monkeypatch.setattr(server_mod, "discover_all", lambda _d=None: [])

        srv = HullForgeServer(tmp_path)
        result = await srv._handle_call_tool(None, Params("hf_ping"))
        assert result.is_error is True

        message = result.content[0].text
        assert "No running Unreal editor" in message
        # The whole point of the rewrite: it must say a restart is unnecessary.
        assert "restart" in message.lower()

    async def test_bridge_error_drops_the_client_for_reconnect(self, wired):
        srv, editor = wired
        editor.raw_override = b"\xff\xff\xff\x7f"  # framing fault

        result = await srv._handle_call_tool(None, Params("t"))
        assert result.is_error is True
        assert srv._client is None  # forces rediscovery next call


class TestImageContent:
    """A screenshot must arrive as a real image block, not base64 text.

    This is the difference between the model seeing a picture and being handed
    a megabyte of characters to read.
    """

    # 1x1 transparent PNG.
    PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )

    @pytest.fixture
    def shot(self, editor):
        @editor.tool("hf_screenshot")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {
                "image_base64": TestImageContent.PNG_B64,
                "mime_type": "image/png",
                "width": 1, "height": 1,
                "source_width": 3840, "source_height": 2160,
                "source": "slate_window",
            }}
        return editor

    async def test_emits_an_image_block(self, wired, shot):
        srv, _ = wired
        result = await srv._handle_call_tool(None, Params("hf_screenshot"))
        images = [c for c in result.content if getattr(c, "type", None) == "image"]
        assert len(images) == 1
        assert images[0].data == self.PNG_B64
        assert images[0].mime_type == "image/png"

    async def test_metadata_travels_as_text_without_the_bytes(self, wired, shot):
        srv, _ = wired
        result = await srv._handle_call_tool(None, Params("hf_screenshot"))
        texts = [c for c in result.content if getattr(c, "type", None) == "text"]
        assert len(texts) == 1
        meta = json.loads(texts[0].text)
        assert meta["source_width"] == 3840
        # The bytes belong in the image block and nowhere else.
        assert "image_base64" not in meta

    async def test_base64_is_stripped_from_structured_content(self, wired, shot):
        # Duplicating it doubles the payload, and some clients drop content[]
        # when structured_content is present - which would lose the picture and
        # leave a megabyte of base64 behind.
        srv, _ = wired
        result = await srv._handle_call_tool(None, Params("hf_screenshot"))
        assert "image_base64" not in result.structured_content
        assert result.structured_content["width"] == 1

    async def test_non_image_tools_are_unaffected(self, wired):
        srv, editor = wired

        @editor.tool("plain")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {"a": 1}}

        result = await srv._handle_call_tool(None, Params("plain"))
        assert all(getattr(c, "type", None) == "text" for c in result.content)
        assert result.structured_content == {"a": 1}

    async def test_empty_image_field_falls_back_to_text(self, wired):
        srv, editor = wired

        @editor.tool("empty_shot")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok",
                    "result": {"image_base64": "", "width": 0}}

        result = await srv._handle_call_tool(None, Params("empty_shot"))
        assert all(getattr(c, "type", None) == "text" for c in result.content)


class TestIdempotency:
    """A retry must not apply a mutation twice."""

    @pytest.fixture
    async def mutating(self, wired):
        srv, editor = wired

        @editor.tool("hf_describe_tools")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {"tools": [
                {"name": "writer", "description": "", "mutating": True,
                 "input_schema": {"type": "object"}},
                {"name": "reader", "description": "", "mutating": False,
                 "input_schema": {"type": "object"}},
            ]}}

        await srv._handle_list_tools(None, None)   # populates _mutating
        return srv, editor

    async def test_mutating_calls_carry_an_operation_id(self, mutating):
        srv, editor = mutating

        @editor.tool("writer")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {}}

        await srv._handle_call_tool(None, Params("writer"))
        sent = [m for m in editor.received if m.get("tool") == "writer"]
        assert len(sent) == 1
        assert sent[0].get("operation_id")

    async def test_read_only_calls_do_not(self, mutating):
        # Reads are naturally idempotent; an id would only cache stale state.
        srv, editor = mutating

        @editor.tool("reader")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {}}

        await srv._handle_call_tool(None, Params("reader"))
        sent = [m for m in editor.received if m.get("tool") == "reader"]
        assert "operation_id" not in sent[0]

    async def test_transport_retry_reuses_the_same_operation_id(self, mutating):
        # This is the crux. A fresh id per attempt would dedupe nothing, so the
        # retry has to live here and hold the id steady.
        srv, editor = mutating
        state = {"n": 0}

        @editor.tool("writer")
        def _(msg):
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionResetError  # drops the connection mid-call
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {"n": state["n"]}}

        result = await srv._handle_call_tool(None, Params("writer"))
        assert result.is_error in (False, None)

        ids = [m["operation_id"] for m in editor.received if m.get("tool") == "writer"]
        assert len(ids) == 2, "expected one retry"
        assert ids[0] == ids[1], "retry must reuse the operation_id or dedupe cannot work"

    async def test_reports_a_replay_to_the_model(self, mutating):
        srv, editor = mutating

        @editor.tool("writer")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok",
                    "result": {"done": True}, "deduplicated": True}

        result = await srv._handle_call_tool(None, Params("writer"))
        assert result.structured_content["deduplicated"] is True
        assert "replayed" in result.content[0].text

    async def test_no_replay_note_on_a_normal_call(self, mutating):
        srv, editor = mutating

        @editor.tool("writer")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {"done": True}}

        result = await srv._handle_call_tool(None, Params("writer"))
        assert "deduplicated" not in result.structured_content
        assert "replayed" not in result.content[0].text

    async def test_gives_up_after_one_retry(self, mutating):
        srv, editor = mutating
        editor.raw_override = b"\xff\xff\xff\x7f"  # permanent framing fault

        result = await srv._handle_call_tool(None, Params("writer"))
        assert result.is_error is True
        assert "Bridge error" in result.content[0].text

    async def test_a_failed_tool_is_not_retried(self, mutating):
        # ToolFailed is a definite answer, not a transport problem.
        srv, editor = mutating

        @editor.tool("writer")
        def _(msg):
            return {"id": msg["id"], "ok": False, "status": "failed", "error": "nope"}

        result = await srv._handle_call_tool(None, Params("writer"))
        assert result.is_error is True
        assert len([m for m in editor.received if m.get("tool") == "writer"]) == 1

    async def test_an_unknown_outcome_is_not_retried(self, mutating):
        # Retrying an unknown outcome is exactly the double-apply we are
        # preventing. The model must verify instead.
        srv, editor = mutating

        @editor.tool("writer")
        def _(msg):
            return {"id": msg["id"], "ok": False, "status": "unknown", "error": "timed out"}

        result = await srv._handle_call_tool(None, Params("writer"))
        assert result.is_error is True
        assert "OUTCOME UNKNOWN" in result.content[0].text
        assert len([m for m in editor.received if m.get("tool") == "writer"]) == 1
