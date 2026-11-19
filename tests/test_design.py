"""The game design tools: pure checks, the simulator, files and tables."""

import asyncio
import json

import pytest

from hullforge_mcp import design as d


def run(coro):
    return asyncio.run(coro)


def conv_ok():
    return {"id": "intro", "start": "a", "nodes": [
        {"id": "a", "speaker": "Mara", "text": "You made it.", "next": "b"},
        {"id": "b", "speaker": "Player", "text": "Barely.", "choices": [
            {"text": "Where now?", "next": "c"}, {"text": "Goodbye.", "end": True}]},
        {"id": "c", "speaker": "Mara", "text": "The old mill.", "end": True}]}


def test_dialogue_clean_graph_passes():
    r = d.check_dialogue(conv_ok())
    assert r["ok"], r


def test_dialogue_finds_every_kind_of_break():
    c = conv_ok()
    c["nodes"][2].pop("end")                              # c is now a dead end
    c["nodes"].append({"id": "orphan", "speaker": "Mara", "text": "Never said."})
    c["nodes"][0]["next"] = "missing"                     # dangling
    r = d.check_dialogue(c)
    assert not r["ok"]
    assert {"from": "a", "to": "missing"} in r["dangling_links"]
    assert "orphan" in r["unreachable"] and "orphan" in r["dead_ends"]


def test_dialogue_loop_with_no_exit():
    c = {"id": "loop", "nodes": [
        {"id": "a", "speaker": "X", "text": "Again?", "next": "b"},
        {"id": "b", "speaker": "X", "text": "Again.", "next": "a"}]}
    assert set(d.check_dialogue(c)["loops_without_exit"]) == {"a", "b"}


def test_bible_flags_misspelt_name_and_lists_facts():
    bible = {"entries": {}}
    d.bible_upsert(bible, [{"kind": "character", "name": "Mara Voss", "aliases": ["Mara"], "facts": ["Mara is a smuggler."]}])
    r = d.check_text(bible, ["Talk to Marra about the cargo.", "Mara knows Kestrel."])
    assert r["misspelled_names"] and r["misspelled_names"][0]["did_you_mean"] == "Mara Voss"
    assert "Kestrel" in r["names_not_in_bible"]
    assert r["facts_to_check"]["Mara Voss"] == ["Mara is a smuggler."]


def test_bible_update_keeps_unspecified_fields():
    bible = {"entries": {}}
    d.bible_upsert(bible, [{"kind": "place", "name": "Old Mill", "facts": ["Burned in the war."]}])
    d.bible_upsert(bible, [{"kind": "place", "name": "Old Mill", "summary": "A ruin."}])
    e = bible["entries"]["old mill"]
    assert e["facts"] == ["Burned in the war."] and e["summary"] == "A ruin."


def test_quest_needs_an_item_nobody_gives():
    q = {"id": "q1", "steps": [
        {"id": "s1", "objective": "Talk to Mara", "gives": ["mill_key"], "next": "s2"},
        {"id": "s2", "objective": "Open the vault", "requires": ["vault_key"], "next": "s3"},
        {"id": "s3", "objective": "Leave", "end": True}]}
    r = d.check_quest(q)
    assert not r["can_finish"] and r["needs_not_given"] == [{"step": "s2", "needs": ["vault_key"]}]
    q["world_items"] = ["vault_key"]
    assert d.check_quest(q)["ok"]


def test_quest_branch_one_path_works():
    q = {"id": "q2", "steps": [
        {"id": "a", "objective": "Choose", "branches": ["b", "c"]},
        {"id": "b", "objective": "Fight", "requires": ["sword"], "end": True},
        {"id": "c", "objective": "Sneak", "end": True}]}
    r = d.check_quest(q)
    assert r["can_finish"] and r["needs_not_given"]


def test_combat_matches_hand_arithmetic():
    # The arena: three drones, 8 damage every 1.2 s, 100 health -> 13 hits -> about 5.2 s.
    r = d.sim_combat([{"name": "drone", "damage": 8, "interval": 1.2, "count": 3}], {"health": 100})
    assert r["total_dps"] == pytest.approx(20.0, abs=0.01)
    assert r["time_to_kill_seconds"] == pytest.approx(4.8, abs=0.01)


def test_combat_armor_and_regen_can_make_target_survive():
    r = d.sim_combat([{"damage": 5, "interval": 1}], {"health": 50, "armor": 2, "regen": 3.5}, seconds=60)
    assert r["survives"]


def test_economy_reports_first_unaffordable_and_inflation():
    r = d.sim_economy([{"name": "loot", "per_minute": 10}], [{"name": "sword", "cost": 500, "at_minutes": [10]}], minutes=60)
    assert r["first_unaffordable"]["wanted"] == "sword"
    assert "inflation" in r.get("note", "")


def test_progression_flags_steep_jump():
    r = d.sim_progression(10, {"type": "exponential", "base": 100, "factor": 2}, 50)
    assert r["steep_jumps_at"]


def test_check_rows_budget_and_ranges():
    rows = [{"Name": "Dagger", "damage": 10, "interval": 0.5}, {"Name": "Club", "damage": 40, "interval": 0.5}]
    r = d.check_rows(rows, [{"budget": "damage / interval", "max": 30}, {"field": "damage", "min": 1}])
    assert [b["name"] for b in r["broken_rules"]] == ["Club"]


def test_budget_refuses_code():
    with pytest.raises(d.DesignError):
        d.check_rows([{"Name": "x", "a": 1}], [{"budget": "__import__('os')", "max": 1}])


def test_names_duplicates_and_bible_clash():
    bible = {"entries": {}}
    d.bible_upsert(bible, [{"kind": "character", "name": "Kestrel"}])
    r = d.check_names(["Brann", "Bran", "brann", "Kestrel"], bible)
    assert r["duplicates"] == ["Brann"] and r["clash_with_bible"]


def test_safe_id_blocks_traversal():
    for bad in ("../x", "a/b", "", ".hidden"):
        with pytest.raises(d.DesignError):
            d.safe_id(bad, "id")


def test_design_doc_round_trip_and_history(tmp_path):
    store = d.Store(tmp_path)
    run(d.call_tool("hf_write_design_doc", {"sections": {"title": "Hull", "pitch": "Salvage ships.", "pillars": ["Tense", "Tactile", "Fair"]}}, store, None))
    run(d.call_tool("hf_write_design_doc", {"sections": {"pitch": "Salvage derelict ships."}}, store, None))
    doc = run(d.call_tool("hf_design_doc", {}, store, None))["doc"]
    assert doc["pitch"] == "Salvage derelict ships." and doc["pillars"] == ["Tense", "Tactile", "Fair"]
    assert list((tmp_path / "HullForge" / "Design" / "history").glob("design-*.json"))
    assert (tmp_path / "HullForge" / "Design" / "design.md").read_text(encoding="utf-8").startswith("# Hull")


def test_unknown_doc_section_refused(tmp_path):
    with pytest.raises(d.DesignError):
        run(d.call_tool("hf_write_design_doc", {"sections": {"plot_twist": "x"}}, d.Store(tmp_path), None))


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.structs, self.tables = {}, {}

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "hf_describe_struct":
            if args["struct"] not in self.structs:
                return False, "not found"
            return True, {"members": [{"name": m} for m in self.structs[args["struct"]]]}
        if name == "hf_create_struct":
            self.structs[args["destination"]] = ["MemberVar_0"]
            return True, {}
        if name == "hf_add_struct_member":
            self.structs[args["struct"]].append(args["member"])
            return True, {}
        if name == "hf_remove_struct_member":
            self.structs[args["struct"]].remove(args["member"])
            return True, {}
        if name == "hf_describe_data_table":
            return (args["table"] in self.tables), {}
        if name == "hf_create_data_table":
            self.tables[args["destination"]] = ""
            return True, {}
        if name == "hf_import_data_table_csv":
            self.tables[args["data_table"]] = args["text"]
            return True, {}
        return False, "unexpected"


def test_dialogue_written_to_table_with_project_struct(tmp_path):
    eng = FakeEngine()
    out = run(d.call_tool("hf_write_dialogue", {"conversation": conv_ok()}, d.Store(tmp_path), eng))
    assert out["engine"]["data_table"] == "/Game/Design/DT_Dialogue"
    s = eng.structs["/Game/Design/S_DialogueLine"]
    assert "MemberVar_0" not in s and "Line" in s and "Choice4Next" in s
    csv_text = eng.tables["/Game/Design/DT_Dialogue"]
    assert csv_text.splitlines()[0].startswith("Name,Conversation,Node,Speaker,Line")
    assert "intro.b" in csv_text and "intro.c" in csv_text


def test_broken_dialogue_refused_unless_allowed(tmp_path):
    c = conv_ok()
    c["nodes"][0]["next"] = "nowhere"
    with pytest.raises(d.DesignError):
        run(d.call_tool("hf_write_dialogue", {"conversation": c}, d.Store(tmp_path), FakeEngine()))
    run(d.call_tool("hf_write_dialogue", {"conversation": c, "allow_problems": True}, d.Store(tmp_path), FakeEngine()))


def test_second_conversation_shares_the_table(tmp_path):
    eng, store = FakeEngine(), d.Store(tmp_path)
    run(d.call_tool("hf_write_dialogue", {"conversation": conv_ok()}, store, eng))
    c2 = conv_ok()
    c2["id"] = "outro"
    run(d.call_tool("hf_write_dialogue", {"conversation": c2}, store, eng))
    text = eng.tables["/Game/Design/DT_Dialogue"]
    assert "intro.a" in text and "outro.a" in text


def test_tuning_table_and_bad_range(tmp_path):
    eng = FakeEngine()
    m = {"name": "Dodge", "summary": "A quick sidestep.", "rules": ["Costs stamina."],
         "values": [{"name": "StaminaCost", "value": 25, "min": 0, "max": 100, "unit": "stamina"},
                    {"name": "IFrames", "value": 0.3, "min": 0, "max": 1, "unit": "s"}]}
    out = run(d.call_tool("hf_write_tuning", {"mechanic": m}, d.Store(tmp_path), eng))
    assert "Dodge.StaminaCost" in eng.tables["/Game/Design/DT_Tuning"]
    assert "read_in_code" in out
    m["values"][0]["value"] = 500
    with pytest.raises(d.DesignError):
        run(d.call_tool("hf_write_tuning", {"mechanic": m}, d.Store(tmp_path), eng))


def test_game_table_rules_gate(tmp_path):
    eng = FakeEngine()
    args = {"table": "DT_Items", "struct": "S_Item", "columns": {"Damage": "float", "Interval": "float"},
            "rows": [{"Name": "Club", "Damage": 40, "Interval": 0.5}],
            "rules": [{"budget": "Damage / Interval", "max": 30}]}
    with pytest.raises(d.DesignError):
        run(d.call_tool("hf_write_game_table", args, d.Store(tmp_path), eng))
    args["rows"][0]["Damage"] = 10
    out = run(d.call_tool("hf_write_game_table", args, d.Store(tmp_path), eng))
    assert out["engine"]["rows"] == 1


def test_blockout_plan_lays_beats_end_to_end():
    plan = d.blockout_plan([{"name": "Entry", "kind": "corridor"}, {"name": "Ambush", "kind": "cover", "enemies": 3},
                            {"name": "Overlook", "kind": "ledge"}])
    assert [p["folder"] for p in plan] == ["Blockout/01_Entry", "Blockout/02_Ambush", "Blockout/03_Overlook"]
    assert plan[1]["start_x_cm"] == pytest.approx(2000 + 200)
    assert sum(b["part"] == "Enemy" for b in plan[1]["boxes"]) == 3
    assert any(b["part"] == "PlayerStart" for b in plan[0]["boxes"])
    assert any(b["part"] == "Riser" for b in plan[2]["boxes"])


def test_blockout_spawns_cubes_into_folders(tmp_path):
    calls = []

    async def eng(name, args):
        calls.append((name, args))
        if name == "hf_spawn_actor_from_asset":
            return True, {"actor": f"Cube{len(calls)}"}
        return True, {}
    out = run(d.call_tool("hf_blockout_from_beats", {"id": "lvl1", "beats": [{"name": "Hall", "kind": "room"}]}, d.Store(tmp_path), eng))
    spawns = [a for n, a in calls if n == "hf_spawn_actor_from_asset"]
    assert out["spawn"]["spawned"] == len(spawns) and all(a["asset_path"].startswith("/Engine/BasicShapes/Cube") for a in spawns)
    assert any(n == "hf_set_actor_folder" and a["folder"].startswith("Blockout/01_Hall") for n, a in calls)


def test_playtest_batch_summarises_runs(tmp_path):
    import itertools
    counter = itertools.count()

    async def pt(args):
        i = next(counter)
        alive = i % 4 != 0
        steps = [{"step": 1, "type": "wait", "status": "passed", "seconds": 2.0},
                 {"step": 2, "type": "expect", "name": "alive", "status": "passed" if alive else "failed",
                  "message": "Health 0", "seconds": 3.0 + i}]
        return True, {"status": "passed" if alive else "failed", "passed": 2 if alive else 1, "steps": steps, "elapsed_seconds": 6 + i}
    out = run(d.call_tool("hf_playtest_batch", {"id": "arena", "runs": 8, "steps": [{"type": "wait", "seconds": 2}],
                                                "sections": [{"name": "fight", "from_step": 1, "to_step": 2}]},
                          d.Store(tmp_path), None, pt))
    assert out["runs"] == 8 and out["passed"] == 6 and out["failed_at_step"] == {"alive": 2}
    assert out["failure_reasons"] == {"Health 0": 2}
    assert out["section_seconds"]["fight"]["completed_runs"] == 6 and out["section_seconds"]["fight"]["min"] == 6.0


def test_sentence_start_word_is_not_a_name():
    r = d.check_text({"entries": {}}, ["Barely. Kestrel waits.", "Run to Kestrel!"])
    assert r["names_not_in_bible"] == ["Kestrel"]


def test_jitter_keeps_shape_and_bounds():
    import random
    steps = [{"type": "wait", "seconds": 2.0}, {"type": "input", "name": "Move", "value": 1.0},
             {"type": "move_to", "location": "100,200,0"}]
    out = d.jitter_steps(steps, 0.3, random.Random(1))
    assert out[0]["seconds"] != 2.0 and -1 <= out[1]["value"] <= 1 and out[2]["location"].endswith(",0")
