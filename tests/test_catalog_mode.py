"""Catalog mode tests.

Progressive tool disclosure: with catalog mode on, the tool list holds a small
core plus whatever areas have been switched on, and the rest are not in the
model's context at all.

Two properties get most of the attention here because getting either wrong
would undo the point of the feature:

  OFF MEANS OFF. Everything else in this product rests on "the tool list is the
  truth about what dispatches". A mode that hides two thirds of the tools has to
  be asked for, and the default has to be indistinguishable from the behaviour
  before the feature existed.

  HIDDEN MEANS UNCALLABLE. A tool that is not listed but still runs makes the
  list advice rather than truth. It is refused, and the refusal names the one
  call that fixes it.
"""

from __future__ import annotations

import mcp.types as types
import pytest

from hullforge_mcp.server import (
    _BRIDGE_STATUS_TOOL,
    _CATALOG_AREAS_ENV,
    _CATALOG_MODE_ENV,
    _DISABLE_AREAS_TOOL,
    _ENABLE_AREAS_TOOL,
    _LIST_AREAS_TOOL,
    HullForgeServer,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


TOOLS = [
    {"name": "hf_describe_tools", "description": "internal", "mutating": False,
     "area": "Core", "input_schema": {"type": "object"}},
    {"name": "hf_get_project_context", "description": "core", "mutating": False,
     "area": "Core", "input_schema": {"type": "object"}},
    {"name": "hf_describe_material", "description": "materials", "mutating": False,
     "area": "Materials", "input_schema": {"type": "object"}},
    {"name": "hf_set_material_parameter", "description": "materials", "mutating": True,
     "area": "Materials", "input_schema": {"type": "object"}},
    {"name": "hf_describe_sequence", "description": "sequencer", "mutating": False,
     "area": "Sequencer", "input_schema": {"type": "object"}},
]

AREAS = [
    {"name": "Core", "tool_count": 1},
    {"name": "Materials", "tool_count": 2},
    {"name": "Sequencer", "tool_count": 1},
]


def describe_reply(msg, *, with_areas: bool = True):
    result = {"tools": TOOLS, "count": len(TOOLS)}
    if with_areas:
        result["areas"] = AREAS
        result["area_count"] = len(AREAS)
    return {"id": msg["id"], "ok": True, "status": "ok", "result": result}


@pytest.fixture
async def wired(editor, tmp_path, monkeypatch):
    from hullforge_mcp import server as server_mod
    from hullforge_mcp.session import SessionInfo

    info = SessionInfo(port=editor.port, token=editor.token, pid=0,
                       project_name="FakeProject")
    monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])

    @editor.tool("hf_describe_tools")
    def _(msg):
        return describe_reply(msg)

    srv = HullForgeServer(tmp_path)
    try:
        yield srv, editor
    finally:
        if srv._client is not None:
            await srv._client.close()


def names(result) -> set[str]:
    return {t.name for t in result.tools}


async def call(srv, name, args=None):
    params = types.CallToolRequestParams(name=name, arguments=args or {})
    return await srv._handle_call_tool(None, params)


class TestOffByDefault:
    async def test_lists_everything_when_off(self, wired, monkeypatch):
        """The default has to be indistinguishable from before the feature."""
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, _ = wired

        listed = names(await srv._handle_list_tools(None, None))
        assert "hf_describe_material" in listed
        assert "hf_describe_sequence" in listed
        assert "hf_get_project_context" in listed

    async def test_does_not_offer_switches_that_would_do_nothing(self, wired, monkeypatch):
        """Listing a tool that is a no-op is worse than not listing it."""
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, _ = wired

        listed = names(await srv._handle_list_tools(None, None))
        assert _LIST_AREAS_TOOL.name in listed, "the overview is useful either way"
        assert _ENABLE_AREAS_TOOL.name not in listed
        assert _DISABLE_AREAS_TOOL.name not in listed

    async def test_calling_a_switch_when_off_explains_rather_than_pretends(
        self, wired, monkeypatch
    ):
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, _ = wired

        result = await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": ["Materials"]})
        assert result.is_error
        assert _CATALOG_MODE_ENV in result.content[0].text

    async def test_nothing_is_gated_when_off(self, wired, monkeypatch):
        """A tool from any area dispatches normally with the mode off."""
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, editor = wired

        @editor.tool("hf_describe_sequence")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {"fine": True}}

        result = await call(srv, "hf_describe_sequence")
        assert not result.is_error


class TestOn:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
        monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    async def test_lists_only_core_to_begin_with(self, wired):
        srv, _ = wired
        listed = names(await srv._handle_list_tools(None, None))

        assert "hf_get_project_context" in listed, "Core is always on"
        assert "hf_describe_material" not in listed
        assert "hf_describe_sequence" not in listed

    async def test_offers_the_switches(self, wired):
        srv, _ = wired
        listed = names(await srv._handle_list_tools(None, None))

        assert {_BRIDGE_STATUS_TOOL.name, _LIST_AREAS_TOOL.name,
                _ENABLE_AREAS_TOOL.name, _DISABLE_AREAS_TOOL.name} <= listed

    async def test_enabling_an_area_lists_its_tools(self, wired):
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": ["Materials"]})
        assert not result.is_error

        listed = names(await srv._handle_list_tools(None, None))
        assert "hf_describe_material" in listed
        assert "hf_set_material_parameter" in listed
        assert "hf_describe_sequence" not in listed, "only what was asked for"

    async def test_disabling_an_area_removes_its_tools(self, wired):
        srv, _ = wired
        await srv._handle_list_tools(None, None)
        await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": ["Materials"]})

        result = await call(srv, _DISABLE_AREAS_TOOL.name, {"areas": ["Materials"]})
        assert not result.is_error

        listed = names(await srv._handle_list_tools(None, None))
        assert "hf_describe_material" not in listed

    async def test_core_cannot_be_disabled(self, wired):
        """Core holds the tools that answer what else is available.

        Turning it off would leave the model with a list it cannot act on and no
        way to ask for more, which is a corner nobody should be able to reach.
        """
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _DISABLE_AREAS_TOOL.name, {"areas": ["Core"]})
        assert not result.is_error
        assert "Core" in result.structured_content["refused"]

        listed = names(await srv._handle_list_tools(None, None))
        assert "hf_get_project_context" in listed

    async def test_matches_an_area_name_case_insensitively(self, wired):
        """A name typed from memory with the wrong case is a typo, not an error."""
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": ["materials"]})
        assert not result.is_error
        assert "Materials" in result.structured_content["enabled_areas"]

    async def test_refuses_an_unknown_area_and_lists_the_real_ones(self, wired):
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": ["Nonsense"]})
        assert result.is_error
        text = result.content[0].text
        assert "Nonsense" in text
        assert "Materials" in text, "the refusal names what does exist"
        assert "Nothing was changed" in text

    async def test_refuses_an_empty_area_list(self, wired):
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": []})
        assert result.is_error


class TestHiddenMeansUncallable:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
        monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    async def test_refuses_a_tool_from_a_disabled_area(self, wired):
        """The whole feature depends on the list being truth, not advice."""
        srv, editor = wired
        await srv._handle_list_tools(None, None)

        @editor.tool("hf_describe_sequence")
        def _(msg):
            raise AssertionError("a hidden tool must not reach the editor")

        result = await call(srv, "hf_describe_sequence")
        assert result.is_error

    async def test_the_refusal_names_the_call_that_fixes_it(self, wired):
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, "hf_describe_sequence")
        text = result.content[0].text
        assert "Sequencer" in text
        assert _ENABLE_AREAS_TOOL.name in text

    async def test_the_same_tool_works_once_its_area_is_enabled(self, wired):
        srv, editor = wired
        await srv._handle_list_tools(None, None)

        @editor.tool("hf_describe_sequence")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {"fine": True}}

        await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": ["Sequencer"]})
        result = await call(srv, "hf_describe_sequence")
        assert not result.is_error


class TestPreloadedAreas:
    async def test_starts_with_the_areas_the_environment_named(
        self, editor, tmp_path, monkeypatch
    ):
        """For the user who already knows what they are working on today."""
        from hullforge_mcp import server as server_mod
        from hullforge_mcp.session import SessionInfo

        monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
        monkeypatch.setenv(_CATALOG_AREAS_ENV, "Materials, Sequencer")

        info = SessionInfo(port=editor.port, token=editor.token, pid=0,
                          project_name="FakeProject")
        monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg)

        srv = HullForgeServer(tmp_path)
        try:
            listed = names(await srv._handle_list_tools(None, None))
            assert "hf_describe_material" in listed
            assert "hf_describe_sequence" in listed
        finally:
            if srv._client is not None:
                await srv._client.close()


class TestListAreas:
    async def test_reports_every_area_with_its_count(self, wired, monkeypatch):
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, _ = wired

        result = await call(srv, _LIST_AREAS_TOOL.name)
        assert not result.is_error

        payload = result.structured_content
        assert payload["catalog_mode"] is False
        assert payload["area_count"] == 3
        by_name = {a["name"]: a for a in payload["areas"]}
        assert by_name["Materials"]["tool_count"] == 2
        assert by_name["Core"]["always_on"] is True

        # Core holds two registered tools and one of them is internal, so the
        # count is what the model can SEE rather than what the editor holds.
        # Quoting the editor's total would advertise a tool that is not listed.
        assert by_name["Core"]["tool_count"] == 1

    async def test_says_how_to_turn_catalog_mode_on_when_it_is_off(
        self, wired, monkeypatch
    ):
        """A feature nobody can find is a feature nobody has."""
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, _ = wired

        result = await call(srv, _LIST_AREAS_TOOL.name)
        assert _CATALOG_MODE_ENV in result.structured_content["how_to_enable"]
        assert _CATALOG_MODE_ENV in result.content[0].text

    async def test_reports_what_is_listed_when_on(self, wired, monkeypatch):
        monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
        monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)
        srv, _ = wired

        await srv._handle_list_tools(None, None)
        result = await call(srv, _LIST_AREAS_TOOL.name)

        payload = result.structured_content
        assert payload["catalog_mode"] is True
        assert payload["tools_listed"] < payload["tools_total"]

        by_name = {a["name"]: a for a in payload["areas"]}
        assert by_name["Core"]["listed"] is True
        assert by_name["Materials"]["listed"] is False

    async def test_works_before_any_catalog_has_been_fetched(self, wired, monkeypatch):
        """Asking what areas exist should answer, not report zero."""
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, _ = wired

        # No _handle_list_tools call first, so nothing has been fetched yet.
        result = await call(srv, _LIST_AREAS_TOOL.name)
        assert not result.is_error
        assert result.structured_content["area_count"] == 3


class TestAreaTravelsInMeta:
    async def test_every_tool_carries_its_area(self, wired, monkeypatch):
        """So a client other than this one can group by area too.

        The docs generator is the immediate consumer. Without this it kept its
        own hand-maintained category list, and that list had drifted until 63 of
        120 tools were filed under "Other" on the published site.
        """
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, _ = wired

        from hullforge_mcp.server import _AREA_META_KEY

        listed = await srv._handle_list_tools(None, None)
        by_name = {t.name: t for t in listed.tools}

        assert by_name["hf_describe_material"].meta[_AREA_META_KEY] == "Materials"
        assert by_name["hf_describe_sequence"].meta[_AREA_META_KEY] == "Sequencer"

    async def test_the_key_is_namespaced(self):
        """MCP reserves the unprefixed space; extensions carry a domain."""
        from hullforge_mcp.server import _AREA_META_KEY

        assert "/" in _AREA_META_KEY
        assert not _AREA_META_KEY.startswith("modelcontextprotocol.io")


class TestOlderEditor:
    async def test_derives_areas_when_the_editor_does_not_report_them(
        self, editor, tmp_path, monkeypatch
    ):
        """An editor built before areas existed still has to work.

        The per-tool field carries the area either way, so the counts are the
        same with or without the summary; the summary only adds area names that
        have no visible tools at all.
        """
        from hullforge_mcp import server as server_mod
        from hullforge_mcp.session import SessionInfo

        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        info = SessionInfo(port=editor.port, token=editor.token, pid=0,
                          project_name="FakeProject")
        monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])

        @editor.tool("hf_describe_tools")
        def _(msg):
            return describe_reply(msg, with_areas=False)

        srv = HullForgeServer(tmp_path)
        try:
            await srv._handle_list_tools(None, None)
            result = await call(srv, _LIST_AREAS_TOOL.name)
            by_name = {a["name"]: a for a in result.structured_content["areas"]}
            # hf_describe_tools is internal and never reaches the catalog, so
            # Core derives as one rather than two.
            assert by_name["Core"]["tool_count"] == 1
            assert by_name["Materials"]["tool_count"] == 2
        finally:
            if srv._client is not None:
                await srv._client.close()

    async def test_files_a_tool_with_no_area_under_uncategorised(
        self, editor, tmp_path, monkeypatch
    ):
        from hullforge_mcp import server as server_mod
        from hullforge_mcp.session import SessionInfo

        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        info = SessionInfo(port=editor.port, token=editor.token, pid=0,
                          project_name="FakeProject")
        monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])

        @editor.tool("hf_describe_tools")
        def _(msg):
            return {"id": msg["id"], "ok": True, "status": "ok", "result": {
                "tools": [{"name": "hf_orphan", "description": "", "mutating": False,
                           "input_schema": {"type": "object"}}],
                "count": 1,
            }}

        srv = HullForgeServer(tmp_path)
        try:
            await srv._handle_list_tools(None, None)
            assert srv._areas["hf_orphan"] == "Uncategorised"
        finally:
            if srv._client is not None:
                await srv._client.close()
