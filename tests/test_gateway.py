"""The gateway: five fixed tools in front of the whole catalog.

What matters, in order:
  THE LIST NEVER CHANGES. Same five tools with or without an editor, whatever
  the editor holds, because the clients that matter read the list once.
  THE RISK SPLIT HOLDS. A destructive tool only runs through hf_destroy with
  confirm=true; a write never runs through hf_read. A mismatch is refused, not
  rerouted, and nothing reaches the editor.
  BAD ARGUMENTS NEVER REACH THE EDITOR, and the refusal carries the schema.
  SEARCH FINDS THE TOOL from plain words.
"""

from __future__ import annotations

import mcp.types as types
import pytest

from hullforge_mcp import gateway
from hullforge_mcp.server import HullForgeServer

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}


TOOLS = [
    {"name": "hf_describe_tools", "description": "internal", "mutating": False, "area": "Core",
     "input_schema": {"type": "object"}},
    {"name": "hf_describe_actor", "description": "Describe a placed actor: its transform, components and tags.",
     "mutating": False, "area": "Actors",
     "input_schema": _obj({"actor": {"type": "string"}}, ["actor"])},
    {"name": "hf_set_actor_visibility",
     "description": "Show or hide a placed actor, in the EDITOR viewport, in the GAME, or both.",
     "mutating": True, "area": "Actors",
     "input_schema": _obj({"actor": {"type": "string"}, "hidden_in_game": {"type": "boolean"}}, ["actor"])},
    {"name": "hf_delete_actor", "description": "Delete a placed actor from the level.",
     "mutating": True, "area": "Actors", "annotations": {"destructiveHint": True},
     "input_schema": _obj({"actor": {"type": "string"}, "confirm": {"type": "boolean"}}, ["actor", "confirm"])},
    {"name": "hf_set_sequence_key", "description": "Set a keyframe on a track in a level sequence.",
     "mutating": True, "area": "Sequencer",
     "input_schema": _obj({"sequence": {"type": "string"},
                           "frame": {"type": "integer", "description": "Frame number on the display rate, e.g. 120."}},
                          ["sequence", "frame"])},
    {"name": "hf_add_foliage_type_to_level", "description": "Register a foliage type in the open level.",
     "mutating": True, "area": "Foliage",
     "input_schema": _obj({"foliage_type": {"type": "string"}}, ["foliage_type"])},
    {"name": "hf_pie_run_playtest", "description": "Run a playtest of steps in PIE.",
     "mutating": True, "area": "PIE",
     "input_schema": _obj({"steps": {"type": "array"}, "wait_seconds": {"type": "integer"}}, ["steps"])},
    {"name": "hf_pie_get_playtest", "description": "The report of a playtest run.",
     "mutating": False, "area": "PIE",
     "input_schema": _obj({"run_id": {"type": "integer"}}, ["run_id"])},
]


def _reply(msg, result):
    return {"id": msg["id"], "ok": True, "status": "ok", "result": result}


@pytest.fixture
async def gw(editor, tmp_path, monkeypatch):
    from hullforge_mcp import server as server_mod
    from hullforge_mcp.session import SessionInfo

    info = SessionInfo(port=editor.port, token=editor.token, pid=0, project_name="FakeProject")
    monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])

    @editor.tool("hf_describe_tools")
    def _(msg):
        return _reply(msg, {"tools": TOOLS, "count": len(TOOLS)})

    for t in TOOLS[1:]:
        editor.tool(t["name"])(lambda msg, n=t["name"]: _reply(msg, {"tool": n, "args": msg.get("args")}))

    srv = HullForgeServer(tmp_path)
    try:
        yield srv, editor
    finally:
        if srv._client is not None:
            await srv._client.close()


async def call(srv, name, args=None):
    return await srv._handle_call_tool(None, types.CallToolRequestParams(name=name, arguments=args or {}))


def sent(editor, tool):
    return [m for m in editor.received if m.get("tool") == tool]


def text(result):
    return result.content[0].text


class TestFixedList:
    async def test_lists_only_the_gateway(self, gw):
        srv, _ = gw
        listed = [t.name for t in (await srv._handle_list_tools(None, None)).tools]
        assert listed == ["hf_bridge_status", *gateway.NAMES, gateway.LOOKUP]

    async def test_gateway_tools_load_up_front_in_claude_code(self, gw):
        srv, _ = gw
        for t in (await srv._handle_list_tools(None, None)).tools[1:]:
            assert (t.meta or {}).get("anthropic/alwaysLoad") is True

    async def test_same_list_with_no_editor(self, tmp_path, monkeypatch):
        from hullforge_mcp import server as server_mod
        monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [])
        srv = HullForgeServer(tmp_path)
        listed = [t.name for t in (await srv._handle_list_tools(None, None)).tools]
        assert listed == ["hf_bridge_status", *gateway.NAMES, gateway.LOOKUP]

    async def test_risk_annotations(self, gw):
        srv, _ = gw
        by = {t.name: t for t in (await srv._handle_list_tools(None, None)).tools}
        assert by["hf_read"].annotations.read_only_hint is True
        assert by["hf_destroy"].annotations.destructive_hint is True
        assert by["hf_write"].annotations.destructive_hint is False


class TestSearch:
    async def test_plain_words(self, gw):
        srv, _ = gw
        rows = (await call(srv, "hf_search_tools", {"query": "hide an actor in game"})).structured_content["results"]
        assert rows[0]["name"] == "hf_set_actor_visibility"
        assert rows[0]["call_with"] == "hf_write"
        assert rows[0]["required"] == {"actor": "string"}  # argument types come with the hit

    async def test_synonym(self, gw):
        srv, _ = gw
        rows = (await call(srv, "hf_search_tools", {"query": "add a keyframe to a level sequence"})).structured_content["results"]
        assert rows[0]["name"] == "hf_set_sequence_key"

    async def test_kind_filter(self, gw):
        srv, _ = gw
        rows = (await call(srv, "hf_search_tools", {"query": "actor", "kind": "destroy"})).structured_content["results"]
        assert [r["name"] for r in rows] == ["hf_delete_actor"]

    async def test_area_listing_and_area_index(self, gw):
        srv, _ = gw
        rows = (await call(srv, "hf_search_tools", {"area": "actors"})).structured_content["results"]
        assert {r["name"] for r in rows} == {"hf_describe_actor", "hf_set_actor_visibility", "hf_delete_actor"}
        areas = (await call(srv, "hf_search_tools", {})).structured_content["areas"]
        assert areas["Actors"] == 3 and "Core" not in areas  # internal tools never indexed

    async def test_unknown_area(self, gw):
        srv, _ = gw
        r = await call(srv, "hf_search_tools", {"query": "x", "area": "Nope"})
        assert r.is_error and "Actors" in text(r)


class TestSchema:
    async def test_returns_schema_and_caller(self, gw):
        srv, _ = gw
        out = (await call(srv, "hf_tool_schema", {"names": ["hf_delete_actor"]})).structured_content["tools"][0]
        assert out["call_with"] == "hf_destroy" and out["input_schema"]["required"] == ["actor", "confirm"]

    async def test_unknown_suggests(self, gw):
        srv, _ = gw
        out = (await call(srv, "hf_tool_schema", {"names": ["hf_describe_actors"]})).structured_content["tools"][0]
        assert out["error"] and "hf_describe_actor" in out["did_you_mean"]


class TestCalls:
    async def test_read(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_read", {"tool": "hf_describe_actor", "args": {"actor": "Rock"}})
        assert not r.is_error
        assert sent(editor, "hf_describe_actor")[0]["args"] == {"actor": "Rock"}

    async def test_write_through_read_is_refused(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_read", {"tool": "hf_set_actor_visibility", "args": {"actor": "Rock"}})
        assert r.is_error and "hf_write" in text(r)
        assert not sent(editor, "hf_set_actor_visibility")

    async def test_destroy_through_write_is_refused(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_write", {"tool": "hf_delete_actor", "args": {"actor": "Rock", "confirm": True}})
        assert r.is_error and "hf_destroy" in text(r)
        assert not sent(editor, "hf_delete_actor")

    async def test_destroy_needs_confirm(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_destroy", {"tool": "hf_delete_actor", "args": {"actor": "Rock"}, "confirm": False})
        assert r.is_error and not sent(editor, "hf_delete_actor")

    async def test_destroy_passes_confirm_to_the_tool(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_destroy", {"tool": "hf_delete_actor", "args": {"actor": "Rock"}, "confirm": True})
        assert not r.is_error
        assert sent(editor, "hf_delete_actor")[0]["args"] == {"actor": "Rock", "confirm": True}

    async def test_write_preview_goes_to_the_preview_tool(self, gw):
        srv, editor = gw
        editor.tool("hf_preview_write")(lambda msg: _reply(msg, {"previewable": True, "changes": []}))
        r = await call(srv, "hf_write", {"tool": "hf_set_actor_visibility", "args": {"actor": "Rock"}, "preview": True})
        assert not r.is_error
        assert not sent(editor, "hf_set_actor_visibility")
        assert sent(editor, "hf_preview_write")[0]["args"] == {
            "tool": "hf_set_actor_visibility", "args": {"actor": "Rock"}}

    async def test_destroy_preview_needs_no_confirm_and_destroys_nothing(self, gw):
        srv, editor = gw
        editor.tool("hf_preview_write")(lambda msg: _reply(msg, {"previewable": True, "changes": []}))
        r = await call(srv, "hf_destroy", {"tool": "hf_delete_actor", "args": {"actor": "Rock"}, "preview": True})
        assert not r.is_error
        assert not sent(editor, "hf_delete_actor")
        # The tool itself still sees confirm, inside the rolled-back run.
        assert sent(editor, "hf_preview_write")[0]["args"]["args"] == {"actor": "Rock", "confirm": True}

    async def test_destroy_without_preview_still_needs_confirm(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_destroy", {"tool": "hf_delete_actor", "args": {"actor": "Rock"}})
        assert r.is_error and not sent(editor, "hf_delete_actor")

    async def test_bad_args_never_reach_the_editor(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_write", {"tool": "hf_set_sequence_key", "args": {"sequence": "S", "frame": "ten"}})
        assert r.is_error
        assert "frame" in text(r) and '"required"' in text(r)  # the schema comes back
        assert not sent(editor, "hf_set_sequence_key")

    async def test_unknown_argument_refused(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_read", {"tool": "hf_describe_actor", "args": {"actor": "R", "colour": 1}})
        assert r.is_error and not sent(editor, "hf_describe_actor")

    async def test_unknown_tool_suggests(self, gw):
        srv, _ = gw
        r = await call(srv, "hf_read", {"tool": "hf_describe_actr", "args": {}})
        assert r.is_error and "hf_describe_actor" in text(r)

    async def test_inner_tool_by_name_is_refused(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_delete_actor", {"actor": "Rock", "confirm": True})
        assert r.is_error and not sent(editor, "hf_delete_actor")


def _fail(msg, error):
    return {"id": msg["id"], "ok": False, "status": "failed", "error": error}


class TestFailureHelp:
    """A refusal carries what to change, and the same refusal twice says so."""

    async def test_refusal_carries_the_arguments_docs(self, gw):
        srv, editor = gw
        editor.tool("hf_set_sequence_key")(lambda msg: _fail(msg, "frame 9000 is past the end of the sequence."))
        r = await call(srv, "hf_write", {"tool": "hf_set_sequence_key", "args": {"sequence": "S", "frame": 9000}})
        assert r.is_error
        assert "From hf_set_sequence_key's schema" in text(r)
        assert "Frame number on the display rate" in text(r)
        assert "FAILED" not in text(r)  # a first failure is not a repeat

    async def test_the_same_failure_twice_is_called_out(self, gw):
        srv, editor = gw
        editor.tool("hf_set_sequence_key")(lambda msg: _fail(msg, "frame 9000 is past the end of the sequence."))
        args = {"tool": "hf_set_sequence_key", "args": {"sequence": "S", "frame": 9000}}
        await call(srv, "hf_write", args)
        r = await call(srv, "hf_write", args)
        assert "HAS NOW FAILED 2 TIMES" in text(r)
        # Different arguments are a new attempt, not a repeat.
        r = await call(srv, "hf_write", {"tool": "hf_set_sequence_key", "args": {"sequence": "S", "frame": 10}})
        assert "FAILED 2 TIMES" not in text(r)

    async def test_friction_is_logged_and_summarised(self, gw, tmp_path):
        import json as _json
        from hullforge_mcp.friction import FrictionLog
        srv, editor = gw
        srv._friction = FrictionLog(tmp_path / "friction")
        editor.tool("hf_set_sequence_key")(lambda msg: _fail(msg, "no such sequence."))
        args = {"tool": "hf_set_sequence_key", "args": {"sequence": "Secret/Path", "frame": 1}}
        await call(srv, "hf_write", args)
        await call(srv, "hf_write", args)
        await call(srv, "hf_read", {"tool": "hf_describe_actor", "args": {"actor": "Rock"}})

        summary = (await call(srv, "hf_bridge_status")).structured_content["friction"]
        assert summary["calls"] == 3 and summary["failed"] == 2 and summary["repeated_failures"] == 1
        assert summary["top_failing"][0] == {"tool": "hf_set_sequence_key", "failures": 2}

        lines = [_json.loads(l) for l in (tmp_path / "friction").glob("*.jsonl").__next__().read_text().splitlines()]
        assert len(lines) == 3 and lines[1]["repeat"] == 2
        assert lines[0]["args"] == ["frame", "sequence"]
        assert "Secret/Path" not in _json.dumps(lines)  # argument values are never logged


class TestPlaytestWait:
    """A short playtest returns its finished report from the one call."""

    async def test_waits_for_the_report(self, gw):
        srv, editor = gw
        polls = {"n": 0}
        editor.tool("hf_pie_run_playtest")(lambda msg: _reply(msg, {"run_id": 7, "steps": 2}))

        def report(msg):
            polls["n"] += 1
            status = "running" if polls["n"] < 2 else "passed"
            return _reply(msg, {"run_id": 7, "status": status})
        editor.tool("hf_pie_get_playtest")(report)
        r = await call(srv, "hf_write", {"tool": "hf_pie_run_playtest", "args": {"steps": [{"type": "wait"}]}})
        assert not r.is_error
        assert r.structured_content["status"] == "passed"
        assert polls["n"] == 2

    async def test_wait_zero_returns_the_run_id(self, gw):
        srv, editor = gw
        editor.tool("hf_pie_run_playtest")(lambda msg: _reply(msg, {"run_id": 8, "steps": 1}))
        r = await call(srv, "hf_write", {"tool": "hf_pie_run_playtest", "args": {"steps": [], "wait_seconds": 0}})
        assert r.structured_content["run_id"] == 8 and not sent(editor, "hf_pie_get_playtest")


class TestFewerCalls:
    async def test_several_queries_in_one_call(self, gw):
        srv, _ = gw
        r = await call(srv, "hf_search_tools", {"queries": ["hide an actor in game", "add a keyframe to a level sequence"]})
        rows = r.structured_content["results"]
        firsts = {}
        for row in rows:
            firsts.setdefault(row["query"], row["name"])
        assert firsts == {"hide an actor in game": "hf_set_actor_visibility",
                          "add a keyframe to a level sequence": "hf_set_sequence_key"}

    async def test_every_hit_shows_the_exact_call(self, gw):
        srv, _ = gw
        r = await call(srv, "hf_search_tools", {"query": "hide an actor in game"})
        assert r.structured_content["results"][0]["call"] == \
            'hf_write {"tool": "hf_set_actor_visibility", "args": {"actor": "<actor>"}}'
        assert 'call: hf_write {"tool": "hf_set_actor_visibility"' in text(r)

    async def test_schema_points_to_top_level_tools(self, gw):
        srv, _ = gw
        r = await call(srv, "hf_tool_schema", {"names": ["hf_bridge_status", "hf_describe_actor"]})
        tools = r.structured_content["tools"]
        assert "directly" in tools[0]["call"] and "error" not in tools[0]
        assert tools[1]["call"].startswith('hf_read {"tool": "hf_describe_actor"')

    async def test_bridge_status_is_preloaded(self, gw):
        srv, _ = gw
        first = (await srv._handle_list_tools(None, None)).tools[0]
        assert first.name == "hf_bridge_status" and first.meta.get("anthropic/alwaysLoad") is True


class TestBridgeStatusConnects:
    async def test_status_connects_to_a_found_editor(self, gw):
        # An agent that checks status first must hear "connected", not
        # "found but not yet connected" - a rehearsal agent stopped on that.
        srv, _ = gw
        assert srv._client is None
        r = await call(srv, "hf_bridge_status")
        assert r.structured_content["connected"] is True
        assert r.structured_content["tool_count"] > 0
        assert text(r).startswith("Connected to FakeProject")


class TestNearestArgument:
    async def test_unknown_argument_names_the_real_one(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_read", {"tool": "hf_describe_actor", "args": {"act": "Rock"}})
        assert r.is_error and "did you mean 'actor'" in text(r)
