"""Catalog presets: "@level" in HULLFORGE_CATALOG_AREAS instead of an area list.

The point of a preset is that a client which reads the tool list once (Claude
Code) gets a short list from launch without the user naming seven areas. So
the tests that matter are: a preset expands to its areas at startup, it mixes
with plain area names, and a preset or area that matches nothing is SAID, not
silently turned into a shorter list.
"""

from __future__ import annotations

import pytest

from hullforge_mcp import presets
from hullforge_mcp.server import (
    _CATALOG_AREAS_ENV,
    _CATALOG_MODE_ENV,
    _LIST_AREAS_TOOL,
    HullForgeServer,
    _preloaded_areas,
)

from test_catalog_mode import call, describe_reply, names

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TestExpand:
    def test_preset_expands_to_its_areas(self):
        areas, unknown = presets.expand(["@materials"])
        assert areas == set(presets.PRESETS["materials"].areas)
        assert unknown == []

    def test_mixes_with_plain_areas(self):
        areas, _ = presets.expand(["@audio", "Sequencer"])
        assert "Sequencer" in areas and "MetaSounds" in areas

    def test_case_insensitive(self):
        assert presets.expand(["@Level"])[0] == presets.expand(["@level"])[0]

    def test_unknown_preset_is_reported_and_adds_nothing(self):
        areas, unknown = presets.expand(["@nope", "Materials"])
        assert areas == {"Materials"}
        assert unknown == ["@nope"]

    def test_env_expands_presets(self, monkeypatch):
        monkeypatch.setenv(_CATALOG_AREAS_ENV, "@vfx")
        assert "Niagara" in _preloaded_areas()

    def test_env_still_unset_means_none(self, monkeypatch):
        monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)
        assert _preloaded_areas() is None

    def test_every_preset_has_a_summary_and_areas(self):
        for p in presets.PRESETS.values():
            assert p.summary and p.areas
            assert len(set(p.areas)) == len(p.areas), f"@{p.name} repeats an area"

    def test_presets_do_not_repeat_always_on_areas(self):
        for p in presets.PRESETS.values():
            assert "Core" not in p.areas and "Changes" not in p.areas


async def _server(editor, tmp_path, monkeypatch, areas_env):
    from hullforge_mcp import server as server_mod
    from hullforge_mcp.session import SessionInfo

    monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
    monkeypatch.setenv(_CATALOG_AREAS_ENV, areas_env)
    info = SessionInfo(port=editor.port, token=editor.token, pid=0, project_name="FakeProject")
    monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])

    @editor.tool("hf_describe_tools")
    def _(msg):
        return describe_reply(msg)

    return HullForgeServer(tmp_path)


class TestServer:
    async def test_preset_lists_its_areas_from_startup(self, editor, tmp_path, monkeypatch):
        srv = await _server(editor, tmp_path, monkeypatch, "@materials")
        try:
            listed = names(await srv._handle_list_tools(None, None))
            assert "hf_describe_material" in listed
            assert "hf_describe_sequence" not in listed
        finally:
            if srv._client is not None:
                await srv._client.close()

    async def test_list_areas_shows_presets_and_problems(self, editor, tmp_path, monkeypatch):
        srv = await _server(editor, tmp_path, monkeypatch, "@materials, @nope, Sequnecer")
        try:
            await srv._handle_list_tools(None, None)
            result = await call(srv, _LIST_AREAS_TOOL.name)
            payload = result.structured_content
            assert {"@materials", "@level"} <= {p["name"] for p in payload["presets"]}
            problems = " ".join(payload["problems"])
            assert "@nope" in problems
            assert "Sequnecer" in problems
            text = result.content[0].text
            assert "@materials" in text and "WARNING" in text
        finally:
            if srv._client is not None:
                await srv._client.close()


class TestToolPresetVariable:
    """HULLFORGE_TOOL_PRESET: one field for the desktop extension's settings."""

    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        for v in (_CATALOG_MODE_ENV, _CATALOG_AREAS_ENV, "HULLFORGE_TOOL_PRESET"):
            monkeypatch.delenv(v, raising=False)

    def test_turns_catalog_mode_on_with_the_preset(self, monkeypatch):
        from hullforge_mcp.server import _catalog_mode_override
        monkeypatch.setenv("HULLFORGE_TOOL_PRESET", "@audio")
        assert _catalog_mode_override() is True
        assert _preloaded_areas() == set(presets.PRESETS["audio"].areas)

    def test_bare_name_works(self, monkeypatch):
        monkeypatch.setenv("HULLFORGE_TOOL_PRESET", "audio")
        assert _preloaded_areas() == set(presets.PRESETS["audio"].areas)

    def test_blank_means_nothing(self, monkeypatch):
        from hullforge_mcp.server import _catalog_mode_override
        monkeypatch.setenv("HULLFORGE_TOOL_PRESET", "  ")
        assert _catalog_mode_override() is None
        assert _preloaded_areas() is None

    def test_catalog_mode_variable_wins(self, monkeypatch):
        from hullforge_mcp.server import _catalog_mode_override
        monkeypatch.setenv("HULLFORGE_TOOL_PRESET", "@audio")
        monkeypatch.setenv(_CATALOG_MODE_ENV, "0")
        assert _catalog_mode_override() is False

    def test_areas_variable_wins(self, monkeypatch):
        monkeypatch.setenv("HULLFORGE_TOOL_PRESET", "@audio")
        monkeypatch.setenv(_CATALOG_AREAS_ENV, "Materials")
        assert _preloaded_areas() == {"Materials"}
