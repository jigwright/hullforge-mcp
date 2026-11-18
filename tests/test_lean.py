"""Results stay lean: they live in the agent's context for the rest of a session."""

from __future__ import annotations

import json

from hullforge_mcp import gateway
from hullforge_mcp.server import HullForgeServer, _fit_json

NOTE = "Nothing here runs the Blueprint. " * 10


def _text(blocks) -> str:
    return blocks[0].text


class TestLeanResults:
    def test_results_are_compact_json(self, tmp_path):
        text = _fit_json({"a": [1, 2], "b": {"c": "d"}})
        assert text == '{"a":[1,2],"b":{"c":"d"}}'

    def test_a_long_note_goes_out_once_per_session(self, tmp_path):
        srv = HullForgeServer(tmp_path)
        first = json.loads(_text(srv._to_content("hf_create_blueprint", {"ok": True, "not_checked": NOTE})))
        second = json.loads(_text(srv._to_content("hf_compile_blueprint", {"ok": True, "not_checked": NOTE})))
        assert first["not_checked"] == NOTE
        assert second["not_checked"] == "(same as the not_checked of hf_create_blueprint earlier)"

    def test_errors_and_values_are_never_shortened(self, tmp_path):
        srv = HullForgeServer(tmp_path)
        for _ in range(2):
            out = json.loads(_text(srv._to_content("t", {"error": NOTE, "value": NOTE})))
            assert out["error"] == NOTE and out["value"] == NOTE

    def test_short_or_different_text_is_untouched(self, tmp_path):
        srv = HullForgeServer(tmp_path)
        srv._to_content("t", {"not_checked": NOTE})
        out = json.loads(_text(srv._to_content("t", {"not_checked": NOTE + "!", "freshness": "short"})))
        assert out["not_checked"] == NOTE + "!" and out["freshness"] == "short"

    def test_the_shared_force_description_is_shown_short(self):
        schema = {"type": "object", "properties": {"force": {"type": "boolean", "description": "x" * 300},
                                                   "path": {"type": "string"}}}
        slim = gateway.slim_schema(schema)
        assert len(slim["properties"]["force"]["description"]) < 100
        assert slim["properties"]["path"] == {"type": "string"}
        assert len(schema["properties"]["force"]["description"]) == 300  # the original is untouched
