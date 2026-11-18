"""Workflow playbooks: design, plan, build, verify.

What matters:
  EVERY CLIENT CAN GET THEM. As MCP prompts, and through hf_search_tools for
  clients that only speak tools, with or without an editor open.
  THEY NAME REAL TOOLS. Every hf_ name in a playbook is a gateway tool or one
  the plugin registers; a renamed tool fails here, not in a user's session.
"""

from __future__ import annotations

import re
from pathlib import Path

import mcp.types as types
import pytest
from mcp.server.lowlevel import NotificationOptions
from mcp.shared.exceptions import MCPError

from hullforge_mcp import gateway, playbooks
from hullforge_mcp.server import HullForgeServer

pytestmark = pytest.mark.anyio

_PLUGIN_SOURCE = Path(__file__).resolve().parents[2] / "HullForge" / "Source"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def srv(tmp_path, monkeypatch):
    from hullforge_mcp import server as server_mod
    monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [])
    return HullForgeServer(tmp_path)


def _names_in(text: str) -> set[str]:
    return set(re.findall(r"\bhf_[a-z0-9_]+", text))


class TestContent:
    def test_four_stages_in_order(self):
        assert playbooks.ORDER == ("design", "plan", "build", "verify")
        assert list(playbooks.PLAYBOOKS) == list(playbooks.ORDER)

    @pytest.mark.parametrize("name", playbooks.ORDER)
    def test_names_only_known_tools(self, name):
        named = _names_in(playbooks.render(name))
        unknown = named - playbooks.KNOWN_TOOLS - playbooks.GATEWAY_TOOLS
        assert not unknown, f"{name} names tools nobody checked: {sorted(unknown)}"

    @pytest.mark.parametrize("name", playbooks.ORDER)
    def test_each_stage_names_tools(self, name):
        assert len(_names_in(playbooks.render(name)) & playbooks.KNOWN_TOOLS) >= 3

    def test_known_tools_are_all_used(self):
        used = set().union(*(_names_in(playbooks.render(n)) for n in playbooks.ORDER))
        assert playbooks.KNOWN_TOOLS <= used

    def test_gateway_tools_are_the_listed_ones(self):
        assert playbooks.GATEWAY_TOOLS == {"hf_bridge_status", *gateway.NAMES, gateway.LOOKUP, gateway.BATCH, gateway.RESTART, gateway.NOTES}

    @pytest.mark.skipif(not _PLUGIN_SOURCE.is_dir(), reason="plugin source not beside the connector")
    def test_known_tools_exist_in_plugin(self):
        registered = set()
        for f in _PLUGIN_SOURCE.rglob("*.cpp"):
            if f.name.endswith(".spec.cpp"):
                continue
            registered |= set(re.findall(r'Name\s*=\s*TEXT\("(hf_[a-z0-9_]+)"\)',
                                         f.read_text(encoding="utf-8", errors="replace")))
        assert registered, "found no tool registrations; the pattern needs updating"
        missing = playbooks.KNOWN_TOOLS - registered
        assert not missing, f"playbooks name tools the plugin lacks: {sorted(missing)}"

    def test_goal_is_filled_in(self):
        assert "Goal: a door that opens when the player is near" in playbooks.render(
            "build", "a door that opens when the player is near")
        assert "not given" in playbooks.render("build", "  ")

    def test_resolve_is_forgiving(self):
        assert playbooks.resolve("Verify") == "verify"
        assert playbooks.resolve("hullforge-plan") == "plan"
        assert playbooks.resolve("deploy") is None

    def test_braces_in_goal_are_safe(self):
        assert "{x}" in playbooks.render("design", "{x}")


class TestPrompts:
    async def test_listed_with_a_goal_argument(self, srv):
        result = await srv._handle_list_prompts(None, None)
        assert [p.name for p in result.prompts] == list(playbooks.ORDER)
        for p in result.prompts:
            assert p.title.startswith("HullForge: ")
            assert [a.name for a in p.arguments] == ["goal"]
            assert p.arguments[0].required is False

    async def test_get_returns_one_user_message(self, srv):
        result = await srv._handle_get_prompt(None, types.GetPromptRequestParams(
            name="verify", arguments={"goal": "jump pad works"}))
        assert len(result.messages) == 1
        msg = result.messages[0]
        assert msg.role == "user"
        assert "Goal: jump pad works" in msg.content.text
        assert "hf_pie_run_playtest" in msg.content.text

    async def test_unknown_is_an_invalid_params_error(self, srv):
        with pytest.raises(MCPError) as exc:
            await srv._handle_get_prompt(None, types.GetPromptRequestParams(name="ship"))
        assert exc.value.code == -32602
        assert "design, plan, build, verify" in exc.value.message

    def test_prompts_capability_is_advertised(self, srv):
        caps = srv.server.get_capabilities(NotificationOptions(), {})
        assert caps.prompts is not None


class TestThroughSearch:
    async def _call(self, srv, args):
        return await srv._handle_call_tool(
            None, types.CallToolRequestParams(name="hf_search_tools", arguments=args))

    async def test_works_with_no_editor(self, srv):
        r = await self._call(srv, {"playbook": "plan", "query": "add a health pickup"})
        assert not r.is_error
        assert "Goal: add a health pickup" in r.content[0].text
        assert {k: r.structured_content[k] for k in ("playbook", "next")} == {"playbook": "plan", "next": "build"}

    async def test_last_stage_has_no_next(self, srv):
        r = await self._call(srv, {"playbook": "verify"})
        assert r.structured_content["next"] is None

    async def test_unknown_is_refused(self, srv):
        r = await self._call(srv, {"playbook": "deploy"})
        assert r.is_error
        assert "design, plan, build, verify" in r.content[0].text

    def test_schema_enum_matches(self):
        search = next(t for t in gateway.TOOLS if t.name == gateway.SEARCH)
        assert tuple(search.input_schema["properties"]["playbook"]["enum"]) == playbooks.ORDER

    def test_instructions_point_at_playbooks(self):
        assert "playbook" in gateway.INSTRUCTIONS
