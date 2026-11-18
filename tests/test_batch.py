"""hf_batch: several calls in one round-trip, with the gateway's checks intact."""

from __future__ import annotations

import pytest

from test_gateway import call, gw, sent, anyio_backend  # noqa: F401 - fixtures

pytestmark = pytest.mark.anyio


class TestBatch:
    async def test_runs_steps_in_order_and_passes_results_along(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_batch", {"steps": [
            {"tool": "hf_describe_actor", "args": {"actor": "Rock"}, "as": "a"},
            {"tool": "hf_set_actor_visibility", "args": {"actor": "$a.args.actor", "hidden_in_game": True}},
            {"tool": "hf_set_sequence_key", "args": {"sequence": "seq for $a.tool", "frame": 3}},
        ]})
        assert not r.is_error and r.structured_content["ran"] == 3
        assert sent(editor, "hf_set_actor_visibility")[0]["args"]["actor"] == "Rock"
        assert sent(editor, "hf_set_sequence_key")[0]["args"]["sequence"] == "seq for hf_describe_actor"

    async def test_stops_at_the_first_failure(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_batch", {"steps": [
            {"tool": "hf_set_actor_visibility", "args": {"actor": "Rock"}},
            {"tool": "hf_set_sequence_key", "args": {"sequence": "s"}},  # 'frame' missing
            {"tool": "hf_describe_actor", "args": {"actor": "Rock"}},
        ]})
        assert r.is_error
        assert r.structured_content["failed"]["step"] == 1 and r.structured_content["not_run"] == 1
        assert not sent(editor, "hf_describe_actor")

    async def test_refuses_destroying_tools(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_batch", {"steps": [
            {"tool": "hf_delete_actor", "args": {"actor": "Rock", "confirm": True}}]})
        assert r.is_error and "hf_destroy" in r.structured_content["failed"]["error"]
        assert not sent(editor, "hf_delete_actor")

    async def test_an_unknown_reference_is_refused_before_the_call(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_batch", {"steps": [
            {"tool": "hf_set_actor_visibility", "args": {"actor": "$nope.label"}}]})
        assert r.is_error and "$nope" in r.structured_content["failed"]["error"]
        assert not sent(editor, "hf_set_actor_visibility")

    async def test_a_missing_field_names_the_fields_that_are_there(self, gw):
        srv, editor = gw
        r = await call(srv, "hf_batch", {"steps": [
            {"tool": "hf_describe_actor", "args": {"actor": "Rock"}, "as": "a"},
            {"tool": "hf_set_actor_visibility", "args": {"actor": "$a.label"}}]})
        err = r.structured_content["failed"]["error"]
        assert "has no 'label'" in err and "tool" in err
        assert not sent(editor, "hf_set_actor_visibility")

    async def test_the_stage_lock_still_holds(self, gw):
        srv, editor = gw
        srv._enter_stage("design")
        r = await call(srv, "hf_batch", {"steps": [
            {"tool": "hf_set_actor_visibility", "args": {"actor": "Rock"}}]})
        assert r.is_error and not sent(editor, "hf_set_actor_visibility")


class TestTerseWrites:
    async def test_a_successful_write_drops_the_caveats_unless_verbose(self, gw):
        srv, editor = gw
        editor.tool("hf_set_actor_visibility")(lambda msg: {
            "id": msg["id"], "ok": True, "status": "ok",
            "result": {"verified": True, "not_checked": "x" * 300, "touched": {"a": 1}, "deprecated": False}})
        short = await call(srv, "hf_write", {"tool": "hf_set_actor_visibility", "args": {"actor": "Rock"}})
        assert short.structured_content == {"verified": True}
        full = await call(srv, "hf_write", {"tool": "hf_set_actor_visibility", "args": {"actor": "Rock"},
                                            "verbose": True})
        assert "touched" in full.structured_content
