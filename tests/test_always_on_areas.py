"""Which areas catalog mode can never hide, and why each one earns it.

Catalog mode exists to keep a hundred and thirty tools out of the model's
context. That is a real saving and the mode is worth having, but every area it
hides is an area the model cannot reach without spending a round trip first.
For almost everything that trade is fine: nobody needs the Sequencer tools until
they are working on a sequence.

Two areas are different, and the bar for joining them is not "useful".

  CORE is where a session starts. Hiding it would leave the model holding a list
  it cannot act on and no way to ask for more.

  CHANGES is the record of what has already been changed. It is what you reach
  for when something has gone wrong, and "switch on the change log first" is the
  worst possible thing to discover at that moment. It is also two tools, so the
  context it costs is nothing against what it saves once.

The refusal has to name the area and give ITS reason. One refusal covering two
areas with one reason leaves the caller guessing which applied, and a refusal
with no reason reads as arbitrary, which is the thing this product does not do.
"""

from __future__ import annotations

import mcp.types as types
import pytest

from hullforge_mcp.server import (
    _ALWAYS_ON_AREAS,
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
    {"name": "hf_get_project_context", "description": "core", "mutating": False,
     "area": "Core", "input_schema": {"type": "object"}},
    {"name": "hf_session_changes", "description": "what changed", "mutating": False,
     "area": "Changes", "input_schema": {"type": "object"}},
    {"name": "hf_clear_session_changes", "description": "forget", "mutating": False,
     "area": "Changes", "input_schema": {"type": "object"}},
    {"name": "hf_describe_material", "description": "materials", "mutating": False,
     "area": "Materials", "input_schema": {"type": "object"}},
]

AREAS = [
    {"name": "Core", "tool_count": 1},
    {"name": "Changes", "tool_count": 2},
    {"name": "Materials", "tool_count": 1},
]


@pytest.fixture
async def wired(editor, tmp_path, monkeypatch):
    from hullforge_mcp import server as server_mod
    from hullforge_mcp.session import SessionInfo

    info = SessionInfo(port=editor.port, token=editor.token, pid=0,
                       project_name="FakeProject")
    monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])

    @editor.tool("hf_describe_tools")
    def _(msg):
        return {"id": msg["id"], "ok": True, "status": "ok",
                "result": {"tools": TOOLS, "count": len(TOOLS),
                           "areas": AREAS, "area_count": len(AREAS)}}

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


class TestTheSetItself:
    def test_holds_exactly_core_and_changes(self):
        """A guard on the set, not a restatement of it.

        Anything added here is an area every session pays for forever, so the
        addition should be a decision somebody made rather than something that
        arrived with a feature.
        """
        assert _ALWAYS_ON_AREAS == frozenset({"Core", "Changes"})


class TestListedWithoutAsking:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
        monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    async def test_changes_is_there_before_anything_is_enabled(self, wired):
        srv, _ = wired
        listed = names(await srv._handle_list_tools(None, None))

        assert "hf_session_changes" in listed
        assert "hf_clear_session_changes" in listed
        assert "hf_get_project_context" in listed
        assert "hf_describe_material" not in listed, "everything else still hidden"

    async def test_an_env_preload_does_not_replace_the_always_on_set(
            self, wired, monkeypatch):
        """Naming one area is a request to add it, not to become the whole list."""
        monkeypatch.setenv(_CATALOG_AREAS_ENV, "Materials")
        srv, _ = wired

        listed = names(await srv._handle_list_tools(None, None))
        assert "hf_describe_material" in listed
        assert "hf_session_changes" in listed
        assert "hf_get_project_context" in listed


class TestCannotBeHidden:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
        monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    async def test_disabling_changes_is_refused_and_says_why(self, wired):
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _DISABLE_AREAS_TOOL.name, {"areas": ["Changes"]})
        assert not result.is_error
        assert "Changes" in result.structured_content["refused"]

        text = result.content[0].text
        assert "Changes cannot be disabled" in text
        # Its own reason, not the one that belongs to Core.
        assert "already been changed" in text
        assert "every session starts" not in text

        listed = names(await srv._handle_list_tools(None, None))
        assert "hf_session_changes" in listed

    async def test_refusing_two_areas_gives_each_its_own_reason(self, wired):
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _DISABLE_AREAS_TOOL.name,
                            {"areas": ["Core", "Changes"]})
        assert not result.is_error
        assert set(result.structured_content["refused"]) == {"Core", "Changes"}

        text = result.content[0].text
        assert "Core cannot be disabled" in text
        assert "Changes cannot be disabled" in text
        assert "every session starts" in text
        assert "already been changed" in text

    async def test_a_mixed_request_still_disables_the_rest(self, wired):
        """Refusing part of a request must not quietly refuse all of it."""
        srv, _ = wired
        await srv._handle_list_tools(None, None)
        await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": ["Materials"]})

        result = await call(srv, _DISABLE_AREAS_TOOL.name,
                            {"areas": ["Materials", "Changes"]})
        assert not result.is_error
        assert result.structured_content["refused"] == ["Changes"]

        listed = names(await srv._handle_list_tools(None, None))
        assert "hf_describe_material" not in listed, "the part that was allowed"
        assert "hf_session_changes" in listed, "the part that was refused"

    async def test_enabling_an_always_on_area_is_a_no_op_and_says_so(self, wired):
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _ENABLE_AREAS_TOOL.name, {"areas": ["Changes"]})
        assert not result.is_error
        assert result.structured_content["changed"] is False
        assert "already" in result.content[0].text


class TestWhatListAreasReports:
    async def test_marks_both_always_on_and_nothing_else(self, wired, monkeypatch):
        monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        result = await call(srv, _LIST_AREAS_TOOL.name)
        by_name = {a["name"]: a for a in result.structured_content["areas"]}

        assert by_name["Core"]["always_on"] is True
        assert by_name["Changes"]["always_on"] is True
        assert by_name["Materials"]["always_on"] is False

    async def test_counts_the_always_on_tools_as_listed(self, wired, monkeypatch):
        monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        payload = (await call(srv, _LIST_AREAS_TOOL.name)).structured_content
        # Core's one plus Changes' two, and nothing else.
        assert payload["tools_listed"] == 3
        assert payload["tools_total"] == 4

    async def test_says_both_are_always_on_when_the_mode_is_off(
            self, wired, monkeypatch):
        """The advice for turning it on has to be true once it is on."""
        monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
        srv, _ = wired
        await srv._handle_list_tools(None, None)

        payload = (await call(srv, _LIST_AREAS_TOOL.name)).structured_content
        assert payload["catalog_mode"] is False
        assert "Core and Changes are always on" in payload["how_to_enable"]
