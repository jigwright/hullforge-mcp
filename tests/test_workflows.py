"""The ship-and-polish workflows, against a scripted editor."""

import asyncio

import pytest

from hullforge_mcp import design as d
from hullforge_mcp import workflows as w


def run(coro):
    return asyncio.run(coro)


class Editor:
    """Answers by tool name; records every call."""

    def __init__(self, answers=None, fail=()):
        self.answers = answers or {}
        self.fail = set(fail)
        self.calls = []
        self.structs, self.tables = {}, {}

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name in self.fail:
            return False, f"{name} failed"
        if name == "hf_describe_struct":
            return (args["struct"] in self.structs), {"members": [{"member": m} for m in self.structs.get(args["struct"], [])]}
        if name == "hf_create_struct":
            self.structs[args["destination"]] = []
            return True, {}
        if name == "hf_add_struct_member":
            self.structs[args["struct"]].append(args["member"])
            return True, {}
        if name == "hf_describe_data_table":
            return (args["table"] in self.tables), {}
        if name == "hf_create_data_table":
            self.tables[args["destination"]] = ""
            return True, {}
        if name == "hf_import_data_table_csv":
            self.tables[args["data_table"]] = args["text"]
            return True, {}
        a = self.answers.get(name, {})
        return True, a(args) if callable(a) else a


def test_ship_check_blocks_on_errors():
    ed = Editor({"hf_audit_cook_readiness": {"findings": [{"severity": "error", "detail": "Map not in cook list"}]}})
    out = run(w.call_tool("hf_ship_check", {}, d.Store("."), ed))
    assert out["verdict"] == "blocked" and "Map not in cook list" in out["blockers"][0]


def test_ship_check_package_refuses_when_blocked_then_starts():
    ed = Editor({"hf_audit_cook_readiness": {"findings": [{"severity": "error", "detail": "x"}]}})
    out = run(w.call_tool("hf_ship_check", {"mode": "package"}, d.Store("."), ed))
    assert "hf_package_project" not in [c[0] for c in ed.calls]
    ed2 = Editor()
    run(w.call_tool("hf_ship_check", {"mode": "package"}, d.Store("."), ed2))
    assert ("hf_package_project", {"platforms": ["Win64"]}) in ed2.calls


def test_ship_report_launches_only_when_packaged():
    ed = Editor({"hf_get_package_report": {"status": "succeeded", "succeeded": True},
                 "hf_check_packaged_startup": {"succeeded": True}})
    out = run(w.call_tool("hf_ship_check", {"mode": "report"}, d.Store("."), ed))
    assert out["verdict"].startswith("ships")


def test_perf_budget_compares_measured_numbers(tmp_path):
    ed = Editor({"hf_profile_run": {"finished": True, "stats": {"frame_ms_avg": 22.0, "game_ms_avg": 5.0, "hitch_count": 1}}})
    out = run(w.call_tool("hf_perf_budget", {}, d.Store(tmp_path), ed))
    over = {o["budget"]: o for o in out["over_budget"]}
    assert "frame_ms" in over and over["frame_ms"]["measured"] == 22.0
    assert {o["budget"] for o in out["within_budget"]} >= {"game_ms", "hitches"}


def test_failed_steps_are_reported_not_fatal(tmp_path):
    ed = Editor(fail={"hf_audit_textures"})
    out = run(w.call_tool("hf_health_report", {}, d.Store(tmp_path), ed))
    assert any(r["tool"] == "hf_audit_textures" and not r["ok"] for r in out["ran"])


def test_tutorial_orders_actions_and_writes_table(tmp_path):
    ed = Editor({"hf_describe_input_mappings": {"mappings": [{"action": "IA_Fire"}, {"action": "IA_Jump"}, {"action": "IA_Move"}]}})
    out = run(w.call_tool("hf_build_tutorial", {}, d.Store(tmp_path), ed))
    assert [r["Action"] for r in out["steps"]] == ["IA_Move", "IA_Jump", "IA_Fire"]
    assert "Tutorial.01" in ed.tables["/Game/Design/DT_Tutorial"]
    assert out["bot_script"][0] == {"type": "input", "name": "IA_Move", "value": 1.0, "seconds": 0.5}


def test_save_system_adds_fields_and_rejects_bad_types(tmp_path):
    ed = Editor()
    out = run(w.call_tool("hf_generate_save_system", {"fields": [{"name": "Health", "type": "float"},
                                                                 {"name": "Bad", "type": "spaceship"}]}, d.Store(tmp_path), ed))
    assert out["fields"] == ["Health"] and out["failed"][0]["field"] == "Bad"
    assert ("hf_create_blueprint", {"parent_class": "/Script/Engine.SaveGame", "destination": "/Game/Design/BP_GameSave", "compile": False}) in ed.calls


def test_telemetry_parses_sections(tmp_path):
    log = tmp_path / "s.log"
    log.write_text("x\n[2026] LogBlueprintUserMessages: HFT|enter|section=Ambush\nHFT|death|section=Ambush|cause=Drone\n"
                   "HFT|enter|section=Ambush\nHFT|complete|section=Ambush|t=40\n", encoding="utf-8")
    out = run(w.call_tool("hf_playtest_feedback", {"files": [str(log)], "predicted": [{"section": "Ambush", "difficulty": 0.8}]},
                          d.Store(tmp_path), None))
    s = out["sections"]["Ambush"]
    assert s["enter"] == 2 and s["deaths"] == 1 and s["deaths_per_attempt"] == 0.5 and s["median_seconds"] == 40
    assert out["vs_prediction"][0]["deaths_per_attempt"] == 0.5


def test_localization_flags_long_lines(tmp_path):
    store = d.Store(tmp_path)
    store.save({"id": "a", "nodes": [{"id": "n", "text": "x" * 80, "end": True}]}, "dialogue", "a.json")
    out = run(w.call_tool("hf_localization_pass", {"max_chars": 90}, store, Editor()))
    assert out["may_not_fit_count"] == 1 and out["pseudo_examples"][0].startswith("[")


def test_store_kit_sizes(tmp_path):
    out = run(w.call_tool("hf_store_kit", {"stores": ["steam", "itch"]}, d.Store(tmp_path), Editor()))
    assert out["sizes"]["steam.header_capsule"] == "920x430" and "itch.cover" in out["sizes"]


def test_accessibility_scaffolds_settings(tmp_path):
    ed = Editor({"hf_describe_input_mappings": {"mappings": [{"action": "IA_Jump"}]}})
    out = run(w.call_tool("hf_accessibility_pass", {}, d.Store(tmp_path), ed))
    assert out["checks"][0]["status"] == "fail"
    assert "ColorblindMode" in ed.tables["/Game/Design/DT_AccessibilitySettings"]


def test_juice_and_multiplayer(tmp_path):
    ed = Editor({"hf_audit_replication": {"findings": [{"detail": "Health not replicated"}]}})
    j = run(w.call_tool("hf_juice_kit", {"values": {"ShakeScale": 0.5}}, d.Store(tmp_path), ed))
    assert j["values"]["ShakeScale"] == 0.5
    m = run(w.call_tool("hf_multiplayer_check", {"checks": [{"actor": "Player", "property": "Health"}]}, d.Store(tmp_path), ed))
    assert m["replication"][0] == 1 and ("hf_stop_pie", {}) in ed.calls
