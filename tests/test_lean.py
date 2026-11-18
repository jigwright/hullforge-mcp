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


class TestWhatTheAgentReads:
    def test_the_lean_copy_is_trimmed_and_shortened(self, tmp_path):
        # Claude Code shows the model structured_content, so the lean copy is
        # the one that has to be small.
        srv = HullForgeServer(tmp_path)
        big = {"actors": [{"label": f"A{i}", "class": "StaticMeshActor" * 3} for i in range(2000)],
               "not_checked": NOTE}
        lean = srv._lean("hf_list_actors", big)
        assert len(json.dumps(lean, separators=(",", ":"))) <= 12000
        assert "_trimmed" in lean and lean["not_checked"] == NOTE
        again = srv._lean("hf_list_actors", {"not_checked": NOTE})
        assert again["not_checked"].startswith("(same as")

    def test_image_bytes_never_reach_the_lean_copy(self, tmp_path):
        srv = HullForgeServer(tmp_path)
        assert "image_base64" not in srv._lean("hf_screenshot", {"image_base64": "x" * 50000, "width": 2})


class TestToolkits:
    def test_a_playbook_lists_its_tools_with_arguments(self):
        from hullforge_mcp import playbooks
        sig = lambda t: None if t == "hf_spawn_actor" else f"{t}(x) -> hf_write"
        text = playbooks.render("build", "goal", sig)
        assert "hf_create_blueprint(x) -> hf_write" in text
        assert "hf_spawn_actor" not in text.split("Tools this stage")[1]  # a tool the editor lacks is left out

    def test_without_a_catalog_there_is_no_toolkit(self):
        from hullforge_mcp import playbooks
        assert "Tools this stage" not in playbooks.render("build", "goal")
