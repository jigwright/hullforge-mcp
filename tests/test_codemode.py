"""hf_run_code: a Starlark script whose tool calls go through the gateway."""

from __future__ import annotations

import pytest

from hullforge_mcp import codemode
from test_gateway import anyio_backend, call, sent  # noqa: F401
from test_research import gw2  # noqa: F401

pytestmark = pytest.mark.anyio


def body(r):
    return r.structured_content


class TestCodeMode:
    async def test_only_the_result_comes_back(self, gw2):
        srv, editor, _ = gw2
        r = await call(srv, "hf_run_code", {"code": (
            'names = ["Rock%d" % i for i in range(3)]\n'
            'for n in names:\n'
            '    write("hf_set_actor_visibility", {"actor": n, "hidden_in_game": True})\n'
            'log("hid", len(names))\n'
            'len(names)\n')})
        assert not r.is_error, body(r)
        assert body(r)["result"] == 3 and body(r)["calls"] == 3 and body(r)["logs"] == ["hid 3"]
        assert len(sent(editor, "hf_set_actor_visibility")) == 3

    async def test_a_string_that_looks_like_a_reference_is_just_text(self, gw2):
        srv, editor, _ = gw2
        r = await call(srv, "hf_run_code", {"code": 'write("hf_set_actor_visibility", {"actor": "$Rock", "hidden_in_game": True})'})
        assert not r.is_error, body(r)
        assert sent(editor, "hf_set_actor_visibility")[0]["args"]["actor"] == "$Rock"

    async def test_destroying_tools_are_refused(self, gw2):
        srv, editor, _ = gw2
        r = await call(srv, "hf_run_code", {"code": 'write("hf_delete_actor", {"actor": "Rock"})'})
        assert r.is_error and "hf_destroy" in body(r)["error"]
        assert not sent(editor, "hf_delete_actor")

    async def test_a_write_through_read_is_refused(self, gw2):
        srv, editor, _ = gw2
        r = await call(srv, "hf_run_code", {"code": 'read("hf_set_actor_visibility", {"actor": "Rock", "hidden_in_game": True})'})
        assert r.is_error and "write()" in body(r)["error"]
        assert not sent(editor, "hf_set_actor_visibility")

    async def test_schema_checks_still_apply_and_check_false_returns_the_error(self, gw2):
        srv, editor, _ = gw2
        r = await call(srv, "hf_run_code", {"code": 'x = write("hf_set_actor_visibility", {"nope": 1}, check=False)\n"error" in x'})
        assert not r.is_error and body(r)["result"] is True
        assert not sent(editor, "hf_set_actor_visibility")

    async def test_a_failure_says_what_already_ran(self, gw2):
        srv, _, _ = gw2
        r = await call(srv, "hf_run_code", {"code": (
            'write("hf_set_actor_visibility", {"actor": "A", "hidden_in_game": True})\n'
            'write("hf_no_such_tool", {})\n')})
        assert r.is_error and "hf_no_such_tool" in body(r)["error"] and "1 tool call" in body(r)["note"]

    async def test_the_stage_lock_holds_inside_a_script(self, gw2):
        srv, editor, _ = gw2
        await call(srv, "hf_search_tools", {"playbook": "design"})
        r = await call(srv, "hf_run_code", {"code": 'write("hf_set_actor_visibility", {"actor": "A", "hidden_in_game": True})'})
        assert r.is_error and not sent(editor, "hf_set_actor_visibility")

    async def test_search_finds_operations(self, gw2):
        srv, _, _ = gw2
        r = await call(srv, "hf_run_code", {"code": '[h["name"] for h in search("hide an actor in game")][:3]'})
        assert not r.is_error and "hf_set_actor_visibility" in body(r)["result"]


class TestSandbox:
    """The worker alone, with a fake HullForge."""

    @staticmethod
    async def ok(_kind, _msg):
        return True, {"ok": True}

    async def test_no_imports_files_or_while(self):
        for code in ("import os", 'load("x.star", "y")', "while True:\n  pass", 'open("C:/x")'):
            r = await codemode.run(code, self.ok)
            assert "error" in r and r["calls"] == 0, code

    async def test_runaway_cpu_is_stopped(self):
        r = await codemode.run("def f():\n  for i in range(100000000):\n    x = i % 7\nf()", self.ok, seconds=0.5)
        assert "error" in r

    async def test_runaway_memory_is_stopped_without_harm(self):
        r = await codemode.run("def f():\n  s = 'x'\n  for i in range(40):\n    s = s + s\n  return len(s)\nf()",
                               self.ok, seconds=30)
        assert "error" in r

    async def test_recursion_is_refused(self):
        r = await codemode.run("def f():\n  return f()\nf()", self.ok)
        assert "error" in r

    async def test_tool_calls_are_capped(self):
        r = await codemode.run("def f():\n  for i in range(500):\n    read('t')\nf()", self.ok)
        assert "error" in r and str(codemode.MAX_CALLS) in r["error"]

    async def test_waiting_on_tools_does_not_use_the_scripts_time(self):
        import anyio

        async def slow(_k, _m):
            await anyio.sleep(0.4)
            return True, 1
        r = await codemode.run("def f():\n  for i in range(4):\n    read('t')\nf()\n'done'", slow, seconds=0.5)
        assert r.get("result") == "done", r
