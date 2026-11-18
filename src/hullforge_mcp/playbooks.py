# Copyright (c) 2026 Jigwright. All rights reserved.
"""Workflow playbooks: design, plan, build, verify.

Each playbook tells a model which HullForge tools to use at one stage of a
piece of work, and in what order. They reach every client two ways:

  * as MCP prompts (prompts/list, prompts/get), which Claude Desktop, Claude
    Code, VS Code and others show as slash commands;
  * through hf_search_tools {"playbook": "<name>"}, for clients that only
    speak tools. That way the tool list stays the same for everyone.

The text names tools the editor really has. tests/test_playbooks.py keeps it
honest: every inner tool a playbook names must be in KNOWN_TOOLS, and that
list is checked against the plugin source when the plugin is beside us.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import mcp.types as types


@dataclass(frozen=True)
class Playbook:
    name: str
    title: str
    summary: str
    body: str


_COMMON = (
    "How to call HullForge: find tools with hf_search_tools (give 'queries' to "
    "find several at once), get their exact arguments with hf_tool_schema (up to "
    "10 per call) unless the list at the end of this playbook already gives "
    "them, then call each through hf_read, hf_write or hf_destroy - never by its "
    "own name. Never guess arguments. Prefer the "
    "batch forms: hf_batch for a run of dependent steps (each step can use an "
    "earlier one's result as $name.field; it compiles what it touched and can "
    "save), hf_edit_blueprint_graph for a whole graph, hf_set_property with "
    "'properties' for several values. A successful write comes back short; pass "
    "verbose=true only when you need the full report. Calls that do not depend on each other "
    "(several reads, several independent writes) go out together in one "
    "message, not one per turn: every turn re-reads the whole conversation. "
    "Look before searching: tools named in this playbook need no search, and "
    "a search's first hits list every argument. hf_batch also takes foreach, "
    "if, assert, select and return, so a check-then-act sequence needs no "
    "turn in between. Compiles and playtests wait for their result; never poll. "
    "List calls take a filter; ask for what you need, not everything. "
    "If hf_bridge_status says no editor is "
    "connected, ask the user to open their Unreal project and stop there."
)

_DESIGN = """\
# Design: decide what to build, against the project as it is

Goal: {goal}

Nothing is changed in this stage, even when the goal says "make", "compile"
or "place": describe how you will do it and stop. HullForge refuses changes
until the build stage. Read, look and ask.

1. Ground yourself in the project.
   - hf_bridge_status: which editor and project you are connected to.
   - hf_get_project_context and hf_describe_project_identity: engine version,
     modules, plugins, project name.
   - hf_get_game_maps_settings: default maps and game mode.
   - hf_list_actors (open level) and hf_find_assets (content you can reuse).
   - hf_describe_input_mappings: the input the player already has.
   - hf_get_selection: what the user is pointing at, if anything.
   - hf_screenshot: what the level looks like now.
2. Learn how the engine does it. Use hf_api_lookup for the classes, functions
   and modules involved, and for known traps in this engine version.
3. Pin down "done" with the user. Turn it into checks a playtest can make:
   a value on an actor (Health >= 100, bIsDead == false), a log line that must
   or must not appear, a picture to look at. Vague goals become measurable
   ones here, not later.
4. Write the design back to the user:
   - what already exists and will be reused, by asset path or actor label;
   - what is new: assets, actors, Blueprints, input, UI;
   - the acceptance checks from step 3;
   - risks: anything that deletes, renames, or touches many assets.
   Ask for a yes before planning work that deletes or reworks existing content.
5. Save the design as the handoff to the next stages: hf_write
   {{"tool": "hf_stage_notes", "args": {{"stage": "design", "text": "<the design,
   with asset paths, labels and the acceptance checks>"}}}}. Later playbooks
   include it, so each stage can start in a fresh conversation.

Next: the plan playbook.
"""

_PLAN = """\
# Plan: turn the design into steps you can run and check

Goal: {goal}

Nothing is changed in this stage either.

1. Break the work into small steps. Each step is one or a few tool calls and
   has an expected result you can read back. Find every tool now with ONE
   hf_search_tools call using 'queries' (one entry per need) and ONE
   hf_tool_schema call for the tools you picked, so the plan holds real tool
   names and real arguments, not guesses. A Blueprint graph is one
   hf_edit_blueprint_graph step (nodes with refs, links "ref.Pin", defaults);
   events come from kind event (BeginPlay, Tick, ActorBeginOverlap) or
   component_event (a component's OnComponentBeginOverlap).
2. Group the steps into batches of related changes (for example: assets, then
   the Blueprint, then placing it in the level, then input). Each batch starts
   with a checkpoint so it can be rolled back on its own.
3. Mark every step that deletes, renames or moves content, or touches many
   assets. Those run with preview=true first, and deletes go through
   hf_destroy with confirm=true.
4. Write the verification now, before building:
   - the automation tests to run, as an hf_run_automation_tests filter
     (hf_list_automation_tests shows what exists);
   - a playtest for hf_pie_run_playtest: input steps, wait_until and expect
     steps for the acceptance checks, op exists with value false for something
     that should be gone (a pickup consumed), set steps to start from a state
     (Health 90 before a cap test), capture steps with look_at for pictures of
     the thing being tested, and an expect_log step with min_verbosity Error
     and absent=true for "no errors".
5. Show the user the plan: batches, risky steps, and the checks. For small,
   easily undone work, go ahead; for anything large or destructive, wait for
   their yes.
6. Save the plan with hf_stage_notes (stage "plan"): the batches, the checks
   and the playtest steps, complete enough to build from without this
   conversation.

Next: the build playbook.
"""

_BUILD = """\
# Build: make the changes, one checked batch at a time

Goal: {goal}

For each batch in the plan:

1. Run the batch as ONE hf_batch call where you can: checkpoint="<label naming
   the batch>", the steps in order, save=true. It compiles every Blueprint it
   touched and reports errors, so no separate compile or save call is needed.
   A graph is one step: hf_edit_blueprint_graph inside the batch. Use separate
   calls only where you must look at a result before deciding the next step.
   The session already has a "session start" checkpoint ('checkpoint' calls
   hf_set_checkpoint for you).
2. Run anything left in order. Before a delete, rename or wide change, call it
   with preview=true and read what it would change. Every write reports what
   it verified: trust it, and stop on the first failure or UNKNOWN outcome.
   After UNKNOWN, read the current state before trying again.
3. Blueprints: build each graph with one hf_edit_blueprint_graph call
   (compile=true); a failed step rolls the whole call back and names the step,
   so fix it and send it again (hf_compile_blueprint recompiles one on its
   own). Set a component's properties with one
   hf_set_property call using 'properties' on the template path that
   hf_add_blueprint_component returned. Fix compile errors before moving on.
4. C++, when the plan calls for it. hf_bridge_status says whether the user
   has turned on Allow source edits; if not, ask them to (Editor Preferences
   > Plugins > HullForge Source Edits) and say what you will change. Then:
   - hf_source_modules, then hf_source_new_class for a new class, or
     hf_source_read and hf_source_patch (dry_run=true first) for an edit;
   - hf_source_add_dependency when a warning names a module Build.cs lacks;
   - write ALL the new or replaced files in ONE call: hf_source_write with
     "files": [{{"path": ..., "content": ...}}, ...]. It writes them in order and
     then compiles them (mode check) and WAITS for the result, so there is no
     hf_get_compile polling; fix each error by its file and line with
     hf_source_patch, then hf_source_compile (it waits too). If the compile is
     refused over a risk finding, show it to the user (hf_source_risk_scan)
     and pass accept only once they agree;
   - to load the code: hf_source_compile mode live_coding (if the user has
     Live Coding on). If its result says restart_required, or the change adds
     a module dependency, or Live Coding did not register a new class, call
     hf_restart_editor: it saves, closes, rebuilds, reopens and reconnects in
     one call. Do not ask the user to restart by hand. Then
     hf_source_quality_gate with the plan's tests, polling
     hf_get_quality_gate.
   hf_source_restore with a write's backup id undoes it; editor undo does not.
5. If the batch went wrong, look before undoing:
   hf_rollback_to_checkpoint with dry_run=true lists what would be undone,
   then call it through hf_destroy with confirm=true. hf_list_checkpoints
   shows what you can go back to; the change journal file it names is the
   record of every write.
6. Show progress with hf_screenshot when the change is visible.
7. When the batch is right, hf_save_all, so tests and the next batch start
   from saved work.

Keep the user told: one line per batch done. If the plan turns out wrong,
stop and say so rather than improvising destructive changes.
When every batch is done, save what was built (asset paths, actor labels,
anything that differs from the plan) with hf_stage_notes (stage "build").

Next: the verify playbook.
"""

_VERIFY = """\
# Verify: prove it works, with evidence

Goal: {goal}

1. Static checks: hf_list_blueprint_errors for every Blueprint you touched.
   hf_find_references on anything you renamed or replaced, to catch broken
   references.
2. Automation tests: hf_run_automation_tests with the plan's filter, then
   poll hf_get_automation_run until it finishes. Report failures with their
   file and line. (It refuses while PIE runs or with unsaved work.)
3. Playtest: hf_pie_run_playtest with start_pie=true and the plan's steps.
   It waits for a short run and returns the finished report (each step's
   pass/fail); poll hf_pie_get_playtest only if it says it is still running.
   hf_pie_get_playtest with image_step fetches a picture. Look at the
   pictures.
4. When a step fails, find out why before changing anything:
   - hf_pie_get_log: what the game logged, filtered by category or text;
   - hf_pie_read: the live values on the player pawn, game state or an actor;
   - hf_pie_capture: a picture from the player's camera now.
   For a hands-on look: hf_start_pie, then hf_pie_inject_input and
   hf_pie_watch, then hf_stop_pie.
   Fix through the build playbook, then run this one again.
5. Report to the user: each acceptance check with pass or fail and its
   evidence (the value read, the log line, the picture), and anything you
   could not test.

Done means every acceptance check passed in a playtest or a test, not that
the writes succeeded.
"""

PLAYBOOKS: dict[str, Playbook] = {
    p.name: p for p in (
        Playbook("design", "Design",
                 "Decide what to build: read the project, look up the engine, agree "
                 "what done means. Changes nothing.", _DESIGN),
        Playbook("plan", "Plan",
                 "Turn a design into small, checkable steps with real tool names, "
                 "checkpoints and the tests to run. Changes nothing.", _PLAN),
        Playbook("build", "Build",
                 "Make the changes in checkpointed batches, previewing risky ones and "
                 "rolling back a batch that goes wrong.", _BUILD),
        Playbook("verify", "Verify",
                 "Prove it works: Blueprint errors, automation tests and a PIE "
                 "playtest, with evidence for each check.", _VERIFY),
    )
}

ORDER = ("design", "plan", "build", "verify")

_GOAL_ARG = types.PromptArgument(
    name="goal",
    description="What you want to make or change, in plain words. Optional.",
    required=False,
)

# Every inner tool a playbook tells the model to use. The tests check each
# playbook names only these, and that each is a real tool in the plugin.
KNOWN_TOOLS = frozenset({
    "hf_get_project_context", "hf_describe_project_identity", "hf_get_game_maps_settings",
    "hf_list_actors", "hf_find_assets", "hf_describe_input_mappings", "hf_get_selection",
    "hf_screenshot", "hf_list_automation_tests", "hf_run_automation_tests",
    "hf_get_automation_run", "hf_pie_run_playtest", "hf_pie_get_playtest",
    "hf_set_checkpoint", "hf_list_checkpoints", "hf_rollback_to_checkpoint",
    "hf_compile_blueprint", "hf_list_blueprint_errors", "hf_save_all",
    "hf_find_references", "hf_pie_get_log", "hf_pie_read", "hf_pie_capture",
    "hf_start_pie", "hf_stop_pie", "hf_pie_inject_input", "hf_pie_watch",
    "hf_source_modules", "hf_source_new_class", "hf_source_read", "hf_source_patch",
    "hf_source_add_dependency", "hf_source_compile", "hf_get_compile",
    "hf_source_risk_scan", "hf_source_quality_gate", "hf_get_quality_gate",
    "hf_source_restore", "hf_edit_blueprint_graph", "hf_set_property",
    "hf_add_blueprint_component", "hf_source_write",
})

# The connector's own tools, which playbooks also name.
GATEWAY_TOOLS = frozenset({
    "hf_bridge_status", "hf_search_tools", "hf_tool_schema", "hf_read",
    "hf_write", "hf_destroy", "hf_api_lookup", "hf_batch", "hf_restart_editor",
    "hf_stage_notes",
})


# THE TOOLS EACH STAGE USUALLY NEEDS, shown with their arguments.
# Measured over three demo sessions: the agents fetched the same thirty-odd
# schemas every time, and searching plus fetching schemas was close to half of
# every character the tools returned - all of it re-read on every later turn.
# A few lines of signatures in the playbook replace most of that.
TOOLKITS: dict[str, list[tuple[str, list[str]]]] = {
    "design": [
        ("Project and level", ["hf_get_project_context", "hf_census_world_actors", "hf_list_actors",
                               "hf_find_assets", "hf_get_actor_bounds", "hf_describe_input_mappings",
                               "hf_get_game_maps_settings", "hf_list_plugins", "hf_screenshot"]),
        ("Navigation", ["hf_describe_world_navigation", "hf_audit_navigation"]),
    ],
    "plan": [
        ("Checks to plan with", ["hf_list_automation_tests", "hf_test_path",
                                 "hf_list_state_tree_node_types", "hf_get_actor_bounds"]),
    ],
    "build": [
        ("Safety and saving", ["hf_set_checkpoint", "hf_rollback_to_checkpoint", "hf_save_all", "hf_screenshot"]),
        ("Blueprints", ["hf_create_blueprint", "hf_add_blueprint_component", "hf_add_blueprint_variable",
                        "hf_set_blueprint_variable", "hf_set_blueprint_defaults", "hf_edit_blueprint_graph",
                        "hf_add_blueprint_function", "hf_compile_blueprint", "hf_list_blueprint_errors"]),
        ("Level", ["hf_spawn_actor", "hf_set_property", "hf_get_property", "hf_add_gameplay_tag"]),
        ("AI", ["hf_create_blackboard", "hf_add_blackboard_key", "hf_create_behavior_tree", "hf_add_bt_node",
                "hf_add_bt_decorator", "hf_add_bt_service", "hf_set_bt_node_property",
                "hf_set_bt_decorator_flow", "hf_describe_behavior_tree"]),
        ("State Trees", ["hf_create_state_tree", "hf_add_state", "hf_add_state_transition",
                         "hf_add_state_tree_node", "hf_set_state_tree_node_property",
                         "hf_bind_state_tree_property", "hf_list_state_tree_node_types",
                         "hf_compile_state_tree"]),
        ("Audio", ["hf_create_metasound_source", "hf_add_metasound_graph_input", "hf_add_metasound_node",
                   "hf_connect_metasound_nodes", "hf_set_metasound_node_input"]),
        ("Navigation", ["hf_build_navigation", "hf_audit_navigation", "hf_test_path"]),
        ("C++", ["hf_source_modules", "hf_source_new_class", "hf_source_write", "hf_source_patch",
                 "hf_source_read", "hf_source_compile", "hf_source_add_dependency"]),
    ],
    "verify": [
        ("Checks", ["hf_list_blueprint_errors", "hf_list_automation_tests", "hf_run_automation_tests",
                    "hf_get_automation_run"]),
        ("Play", ["hf_pie_run_playtest", "hf_pie_get_playtest", "hf_pie_get_log", "hf_pie_read",
                  "hf_pie_capture", "hf_pie_get_behavior_tree", "hf_start_pie", "hf_stop_pie"]),
    ],
}

_TOOLKIT_HEAD = (
    "Tools this stage usually needs, each as: caller tool {argument:type}. Call "
    "them as hf_write {\"tool\": \"hf_add_state\", \"args\": {...}} - the "
    "caller is the only tool name you call, never the tool's own; inside "
    "hf_batch a step names only the tool. "
    "? marks an optional argument. Call these straight away; search only for "
    "anything not listed, and fetch hf_tool_schema only for a list or obj "
    "argument whose form you have not used before."
)


def render(name: str, goal: str | None = None,
           signature: "Callable[[str], str | None] | None" = None, notes: str = "") -> str:
    """A playbook's full text, with the goal filled in.

    'signature' turns a tool name into its one-line signature from the live
    catalog (None when the editor does not have that tool); without it the
    toolkit is left out."""
    p = PLAYBOOKS[name]
    goal = (goal or "").strip() or "(not given: ask the user, or take it from the conversation)"
    stage = ORDER.index(name) + 1
    header = f"HullForge playbook {stage} of {len(ORDER)}: {' -> '.join(ORDER)}\n\n"
    text = header + p.body.format(goal=goal) + "\n" + _COMMON + "\n"
    if notes.strip():
        text += ("\nNotes saved by the earlier stages (hf_stage_notes). They are the "
                 "design and plan to work from:\n\n" + notes.strip() + "\n")
    if signature is not None and name in TOOLKITS:
        lines = []
        for group, tools in TOOLKITS[name]:
            sigs = [x for x in (signature(t) for t in tools) if x]
            if sigs:
                lines.append(f"{group}:")
                lines += [f"  {x}" for x in sigs]
        if lines:
            text += "\n" + _TOOLKIT_HEAD + "\n" + "\n".join(lines) + "\n"
    return text


def resolve(name: str) -> str | None:
    """The playbook a name means, tolerating case and a 'hullforge-' prefix."""
    key = name.strip().lower().removeprefix("hullforge-").removeprefix("hullforge_")
    return key if key in PLAYBOOKS else None


def list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(name=PLAYBOOKS[n].name, title=f"HullForge: {PLAYBOOKS[n].title}",
                     description=PLAYBOOKS[n].summary, arguments=[_GOAL_ARG])
        for n in ORDER
    ]


def get_prompt(name: str, arguments: dict[str, str] | None,
               signature: "Callable[[str], str | None] | None" = None,
               notes: str = "") -> types.GetPromptResult:
    key = resolve(name)
    if key is None:
        raise ValueError(f"No playbook named '{name}'. Playbooks: {', '.join(ORDER)}.")
    goal = (arguments or {}).get("goal")
    return types.GetPromptResult(
        description=PLAYBOOKS[key].summary,
        messages=[types.PromptMessage(
            role="user",
            content=types.TextContent(type="text", text=render(key, goal, signature, notes)),
        )],
    )


def next_stage(name: str) -> str | None:
    i = ORDER.index(name)
    return ORDER[i + 1] if i + 1 < len(ORDER) else None


def overview() -> str:
    """One line per playbook, for hf_search_tools with no query."""
    return "\n".join(f"  {n}: {PLAYBOOKS[n].summary}" for n in ORDER)
