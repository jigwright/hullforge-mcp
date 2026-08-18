"""Integration tests against a LIVE Unreal editor.

Skipped unless HULLFORGE_PROJECT_DIR is set and a session file is present.
Run with:  uv run pytest -m integration

These are the only tests that can catch drift between the C++ registry and
this side of the wire. Everything else uses a fake.
"""

from __future__ import annotations

import os

import pytest

from hullforge_mcp.client import BridgeClient
from hullforge_mcp.server import HullForgeServer
from hullforge_mcp.session import SessionError, discover

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PROJECT_DIR = os.environ.get("HULLFORGE_PROJECT_DIR", "")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def live_session():
    if not PROJECT_DIR:
        pytest.skip("HULLFORGE_PROJECT_DIR not set")
    try:
        return discover(PROJECT_DIR)
    except SessionError as exc:
        pytest.skip(f"no live editor: {exc}")


class Params:
    def __init__(self, name, arguments=None):
        self.name = name
        self.arguments = arguments


async def test_handshake_and_ping(live_session):
    c = BridgeClient(live_session)
    await c.connect()
    try:
        assert c.server_version
        result = await c.call("hf_ping")
        assert result["server"] == "hullforge"
        assert result["tool_count"] >= 3
    finally:
        await c.close()


async def test_game_thread_tool_round_trips(live_session):
    c = BridgeClient(live_session)
    await c.connect()
    try:
        ctx = await c.call("hf_get_project_context")
        assert ctx["project_name"]
        assert ctx["engine_version"].startswith("5.8")
    finally:
        await c.close()


async def test_catalog_contract_matches_the_editor(live_session):
    """Every advertised MCP tool must be one the editor will actually dispatch,
    and must carry a real object schema. This is the drift detector."""
    srv = HullForgeServer(PROJECT_DIR)
    try:
        result = await srv._handle_list_tools(None, None)
        assert result.tools, "editor advertised no tools"

        client = await srv._ensure_connected()
        described = await client.call("hf_describe_tools")
        editor_names = {t["name"] for t in described["tools"]}

        for tool in result.tools:
            assert tool.name in editor_names, f"{tool.name} not in the editor registry"
            assert isinstance(tool.input_schema, dict)
            assert tool.input_schema.get("type") == "object", (
                f"{tool.name} schema is not an object schema"
            )
            assert tool.annotations is not None
    finally:
        if srv._client is not None:
            await srv._client.close()


async def test_unknown_tool_is_an_error_result_not_an_exception(live_session):
    srv = HullForgeServer(PROJECT_DIR)
    try:
        result = await srv._handle_call_tool(None, Params("hf_definitely_not_a_tool"))
        assert result.is_error is True
        assert "Unknown tool" in result.content[0].text
    finally:
        if srv._client is not None:
            await srv._client.close()
