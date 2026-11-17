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
    "10 per call), then call each through hf_read, hf_write or hf_destroy as its "
    "'call' line shows - never by its own name. Never guess arguments. Prefer the "
    "batch forms: hf_edit_blueprint_graph for a whole graph, hf_set_property with "
    "'properties' for several values. If hf_bridge_status says no editor is "
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

Next: the build playbook.
"""

_BUILD = """\
# Build: make the changes, one checked batch at a time

Goal: {goal}

For each batch in the plan:

1. hf_set_checkpoint with a label naming the batch ("before enemy Blueprint").
   The session already has a "session start" checkpoint.
2. Run the steps in order. Before a delete, rename or wide change, call it
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
   - hf_source_compile (mode check) and poll hf_get_compile; fix each error
     by its file and line. If the compile is refused over a risk finding,
     show it to the user (hf_source_risk_scan) and pass accept only once
     they agree;
   - mode live_coding loads the change into the running editor (if the user
     has Live Coding on), new classes included: after it succeeds, spawn or
     find the class to confirm it is there before asking for a restart. Then
     hf_source_quality_gate with the plan's tests, polling
     hf_get_quality_gate. Only a new MODULE, or a class Live Coding did not
     register, needs a full build and an editor restart: tell the user.
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
    "hf_add_blueprint_component",
})

# The connector's own tools, which playbooks also name.
GATEWAY_TOOLS = frozenset({
    "hf_bridge_status", "hf_search_tools", "hf_tool_schema", "hf_read",
    "hf_write", "hf_destroy", "hf_api_lookup",
})


def render(name: str, goal: str | None = None) -> str:
    """A playbook's full text, with the goal filled in."""
    p = PLAYBOOKS[name]
    goal = (goal or "").strip() or "(not given: ask the user, or take it from the conversation)"
    stage = ORDER.index(name) + 1
    header = f"HullForge playbook {stage} of {len(ORDER)}: {' -> '.join(ORDER)}\n\n"
    return header + p.body.format(goal=goal) + "\n" + _COMMON + "\n"


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


def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    key = resolve(name)
    if key is None:
        raise ValueError(f"No playbook named '{name}'. Playbooks: {', '.join(ORDER)}.")
    goal = (arguments or {}).get("goal")
    return types.GetPromptResult(
        description=PLAYBOOKS[key].summary,
        messages=[types.PromptMessage(
            role="user",
            content=types.TextContent(type="text", text=render(key, goal)),
        )],
    )


def next_stage(name: str) -> str | None:
    i = ORDER.index(name)
    return ORDER[i + 1] if i + 1 < len(ORDER) else None


def overview() -> str:
    """One line per playbook, for hf_search_tools with no query."""
    return "\n".join(f"  {n}: {PLAYBOOKS[n].summary}" for n in ORDER)
