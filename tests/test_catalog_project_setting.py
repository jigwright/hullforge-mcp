"""Catalog mode as a PROJECT setting, and how it composes with the environment.

Catalog mode used to be reachable only by setting an environment variable on the
connector process, which is not something most Unreal developers will do. It can
now also be asked for in the editor, at Project Settings > Plugins > HullForge,
and it travels in the session file for the same reason the port does: one source
of truth, so there is no client-side setting that can disagree with the editor.

Two properties get the attention here.

  THE ENVIRONMENT WINS WHEN IT SAYS ANYTHING. Setting a variable on the
  connector is a deliberate per-launch choice by one person; the project setting
  is committed and shared. Someone who wants every tool today has to be able to
  get them without editing the project, which means the variable has to be able
  to say OFF and not merely ON. That is why the override is tri-state, and it is
  the thing a plain bool would have made impossible.

  SAYING NOTHING IS NOT SAYING OFF. An unset variable, and a variable holding a
  typo, both defer to the project. Treating either as "off" would silently
  ignore a project that asked for the mode.
"""

from __future__ import annotations

import pytest

from hullforge_mcp.server import (
    _CATALOG_AREAS_ENV,
    _CATALOG_MODE_ENV,
    _catalog_mode_override,
    _preloaded_areas,
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


def describe_reply(msg):
    return {"id": msg["id"], "ok": True, "status": "ok",
            "result": {"tools": TOOLS, "count": len(TOOLS),
                       "areas": AREAS, "area_count": len(AREAS)}}


@pytest.fixture
def project(editor, tmp_path, monkeypatch):
    """A server whose editor advertises whatever catalog settings a test wants."""
    from hullforge_mcp import server as server_mod
    from hullforge_mcp.session import SessionInfo

    @editor.tool("hf_describe_tools")
    def _(msg):
        return describe_reply(msg)

    def make(*, catalog_mode: bool = False, catalog_areas: tuple[str, ...] = ()):
        info = SessionInfo(port=editor.port, token=editor.token, pid=0,
                           project_name="FakeProject",
                           catalog_mode=catalog_mode,
                           catalog_areas=catalog_areas)
        monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [info])
        return HullForgeServer(tmp_path)

    return make


async def listed(srv) -> set[str]:
    result = await srv._handle_list_tools(None, None)
    return {t.name for t in result.tools}


# --------------------------------------------------------------- the override

def test_override_is_none_when_unset(monkeypatch):
    monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
    assert _catalog_mode_override() is None


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_override_reads_trueish(monkeypatch, raw):
    monkeypatch.setenv(_CATALOG_MODE_ENV, raw)
    assert _catalog_mode_override() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " Off "])
def test_override_reads_falseish(monkeypatch, raw):
    # The half that a plain bool could not express, and the half that lets one
    # person turn the mode off on a project that has it on.
    monkeypatch.setenv(_CATALOG_MODE_ENV, raw)
    assert _catalog_mode_override() is False


@pytest.mark.parametrize("raw", ["", "  ", "maybe", "1 or so", "onn"])
def test_override_defers_on_anything_it_does_not_understand(monkeypatch, raw):
    # Deferring rather than guessing. A typo that silently meant "off" would
    # ignore a project that asked for the mode, with nothing to say why.
    monkeypatch.setenv(_CATALOG_MODE_ENV, raw)
    assert _catalog_mode_override() is None


def test_preloaded_areas_is_none_when_unset(monkeypatch):
    monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)
    assert _preloaded_areas() is None


def test_preloaded_areas_empty_string_is_not_unset(monkeypatch):
    # "start with nothing but Core" overriding a project list, which is a
    # different instruction from "say nothing and use the project's".
    monkeypatch.setenv(_CATALOG_AREAS_ENV, "")
    assert _preloaded_areas() == set()


# ------------------------------------------------------------- the precedence

async def test_project_setting_turns_the_mode_on(project, monkeypatch):
    monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
    monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    srv = project(catalog_mode=True)
    try:
        names = await listed(srv)
        assert "hf_describe_material" not in names
        assert "hf_get_project_context" in names        # Core is always on
    finally:
        if srv._client is not None:
            await srv._client.close()


async def test_project_setting_off_lists_everything(project, monkeypatch):
    monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
    monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    srv = project(catalog_mode=False)
    try:
        names = await listed(srv)
        assert "hf_describe_material" in names
        assert "hf_describe_sequence" in names
    finally:
        if srv._client is not None:
            await srv._client.close()


async def test_environment_on_beats_project_off(project, monkeypatch):
    monkeypatch.setenv(_CATALOG_MODE_ENV, "1")
    monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    srv = project(catalog_mode=False)
    try:
        names = await listed(srv)
        assert "hf_describe_material" not in names
    finally:
        if srv._client is not None:
            await srv._client.close()


async def test_environment_off_beats_project_on(project, monkeypatch):
    # The case the whole tri-state exists for. One person on a project that
    # ships catalog mode has to be able to get every tool back without editing
    # the project and committing it.
    monkeypatch.setenv(_CATALOG_MODE_ENV, "0")
    monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    srv = project(catalog_mode=True, catalog_areas=("Materials",))
    try:
        names = await listed(srv)
        assert "hf_describe_material" in names
        assert "hf_describe_sequence" in names
    finally:
        if srv._client is not None:
            await srv._client.close()


async def test_a_typo_in_the_variable_defers_to_the_project(project, monkeypatch):
    monkeypatch.setenv(_CATALOG_MODE_ENV, "yess")
    monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    srv = project(catalog_mode=True)
    try:
        names = await listed(srv)
        assert "hf_describe_material" not in names
    finally:
        if srv._client is not None:
            await srv._client.close()


# ------------------------------------------------------------------- the areas

async def test_project_areas_are_listed_from_the_start(project, monkeypatch):
    monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
    monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    srv = project(catalog_mode=True, catalog_areas=("Materials",))
    try:
        names = await listed(srv)
        assert "hf_describe_material" in names       # named by the project
        assert "hf_describe_sequence" not in names   # not named
    finally:
        if srv._client is not None:
            await srv._client.close()


async def test_environment_areas_replace_project_areas(project, monkeypatch):
    monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
    monkeypatch.setenv(_CATALOG_AREAS_ENV, "Sequencer")

    srv = project(catalog_mode=True, catalog_areas=("Materials",))
    try:
        names = await listed(srv)
        assert "hf_describe_sequence" in names
        assert "hf_describe_material" not in names
    finally:
        if srv._client is not None:
            await srv._client.close()


async def test_empty_environment_areas_override_a_project_list(project, monkeypatch):
    # Not the same as unset. This one says "Core only" out loud.
    monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
    monkeypatch.setenv(_CATALOG_AREAS_ENV, "")

    srv = project(catalog_mode=True, catalog_areas=("Materials",))
    try:
        names = await listed(srv)
        assert "hf_describe_material" not in names
        assert "hf_get_project_context" in names
    finally:
        if srv._client is not None:
            await srv._client.close()


async def test_areas_are_seeded_once_and_not_on_reconnect(project, monkeypatch):
    # Re-seeding on every reconnect would undo areas the caller switched on by
    # hand the moment the editor blipped, which from the client side is
    # indistinguishable from the tool list randomly shrinking.
    monkeypatch.delenv(_CATALOG_MODE_ENV, raising=False)
    monkeypatch.delenv(_CATALOG_AREAS_ENV, raising=False)

    srv = project(catalog_mode=True, catalog_areas=("Materials",))
    try:
        await listed(srv)
        srv._enabled_areas.add("Sequencer")
        srv._enabled_areas.discard("Materials")

        # Force a reconnect, which re-reads the session.
        if srv._client is not None:
            await srv._client.close()
        srv._client = None

        names = await listed(srv)
        assert "hf_describe_sequence" in names       # kept what the caller chose
        assert "hf_describe_material" not in names   # not re-seeded from project
    finally:
        if srv._client is not None:
            await srv._client.close()


# --------------------------------------------------------------- the session

def test_session_file_without_the_fields_reads_as_off(tmp_path):
    # An older editor writes no catalog fields at all. That has to read as the
    # behaviour before the feature existed, or upgrading the connector alone
    # would change what an untouched project does.
    import json
    from hullforge_mcp.session import read_session

    path = tmp_path / "session.json"
    path.write_text(json.dumps({
        "port": 8765, "token": "t" * 64, "protocol": 1,
        "project_name": "Old", "pid": 0, "project_dir": str(tmp_path),
    }))

    info = read_session(path)
    assert info.catalog_mode is False
    assert info.catalog_areas == ()


def test_session_file_carries_the_fields(tmp_path):
    import json
    from hullforge_mcp.session import read_session

    path = tmp_path / "session.json"
    path.write_text(json.dumps({
        "port": 8765, "token": "t" * 64, "protocol": 1,
        "project_name": "New", "pid": 0, "project_dir": str(tmp_path),
        "catalog_mode": True,
        "catalog_areas": ["Materials", "  ", "Sequencer"],
    }))

    info = read_session(path)
    assert info.catalog_mode is True
    # Blank entries come from someone adding an array row in the details panel
    # and not typing in it, and an area named "" matches nothing.
    assert info.catalog_areas == ("Materials", "Sequencer")
