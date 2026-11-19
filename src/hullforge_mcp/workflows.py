"""Workflows that string the editor's own tools into one answer.

HullForge has thousands of narrow tools: audits, profilers, the packager, the
localization dashboard. Someone shipping a game asks wider questions: "will
this package?", "is it fast enough?", "is it accessible?", "what should I fix
this week?". Each tool here runs the right narrow tools in order, keeps going
when one of them is missing or fails (and says so), and returns a short
verdict on top of the evidence.

Like design.py, these run in the connector and reach the editor only through
its tools, so every rule those tools enforce still applies.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from .design import (DesignError, EngineCall, Store, ToolDef, WRITE, READ, _obj, _S, safe_id, write_table,
                     _text_list, doc_findings)

AREA = "Ship & Polish"


# ------------------------------------------------------------------ helpers


class Run:
    """The calls one workflow made: each result kept, failures noted, never fatal."""

    def __init__(self, call: EngineCall) -> None:
        self.call = call
        self.ran: list[dict[str, Any]] = []

    async def __call__(self, tool: str, args: dict[str, Any]) -> Any:
        ok, r = await self.call(tool, args)
        self.ran.append({"tool": tool, "ok": ok, **({} if ok else {"error": str(r)[:300]})})
        return r if ok else None


def findings_of(result: Any, limit: int = 15) -> tuple[int, list[Any]]:
    """The count and first entries of the findings an audit returned, whatever it calls them."""
    if not isinstance(result, dict):
        return 0, []
    for key in ("findings", "issues", "problems", "items", "offenders", "results", "rows"):
        v = result.get(key)
        if isinstance(v, list):
            n = result.get("finding_count") or result.get("count") or len(v)
            return int(n) if isinstance(n, (int, float)) else len(v), v[:limit]
    n = result.get("finding_count")
    return (int(n), []) if isinstance(n, (int, float)) else (0, [])


def numbers(obj: Any, prefix: str = "") -> dict[str, float]:
    """Every number in a nested result, by dotted path."""
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(numbers(v, f"{prefix}{k}."))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix.rstrip(".")] = float(obj)
    return out


def pick(nums: dict[str, float], *words: str) -> tuple[str, float] | None:
    """The first number whose path contains every word."""
    for path, v in nums.items():
        p = path.lower()
        if all(w in p for w in words):
            return path, v
    return None


async def wait_task(run: Run, getter: str, args: dict[str, Any], seconds: float, done_key: str = "status",
                    done_values: tuple[str, ...] = ("finished", "succeeded", "complete", "completed", "failed", "error", "passed")) -> Any:
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        ok, r = await run.call(getter, args)
        last = r if ok else None
        st = str((r or {}).get(done_key) or "").lower() if ok and isinstance(r, dict) else ""
        if (ok and st in done_values) or (ok and isinstance(r, dict) and r.get("finished") is True):
            return r
        await asyncio.sleep(2.0)
    return last


# ------------------------------------------------------------- 1 ship check


async def ship_check(run: Run, args: dict[str, Any]) -> dict[str, Any]:
    platform = str(args.get("platform") or "Win64")
    mode = str(args.get("mode") or "check")
    out: dict[str, Any] = {"platform": platform, "mode": mode}
    if mode in ("check", "package"):
        shipping = await run("hf_audit_shipping_settings", {})
        cook = await run("hf_audit_cook_readiness", {"limit": 40})
        uncooked = await run("hf_find_uncooked_references", {"limit": 40, "hard_only": True})
        size = await run("hf_estimate_package_size", {"platforms": [platform]})
        blockers = []
        for label, res in (("shipping settings", shipping), ("cook readiness", cook), ("uncooked references", uncooked)):
            n, first = findings_of(res, 8)
            errs = [f for f in first if isinstance(f, dict) and str(f.get("severity", "")).lower() in ("error", "critical", "blocker")]
            out[label.replace(" ", "_")] = {"findings": n, "first": first}
            if errs:
                blockers += [f"{label}: {e.get('detail') or e.get('message') or e}" for e in errs[:5]]
        out["estimated_size"] = size
        out["blockers"] = blockers
        out["verdict"] = "blocked" if blockers else "ready to package"
        if mode == "package":
            if blockers and not args.get("package_anyway"):
                out["next"] = "Fix the blockers first, or pass package_anyway: true."
                return out
            maps = _text_list(args.get("maps"))
            started = await run("hf_package_project", {"platforms": [platform], **({"maps": maps} if maps else {})})
            out["package"] = started
            out["next"] = ("Packaging runs in the background (often 5 to 30 minutes). Call hf_ship_check again with "
                           "mode report to read the log and smoke-test the build.")
        return out
    if mode == "report":
        report = await run("hf_get_package_report", {"top": 20})
        out["package_report"] = report
        status = str((report or {}).get("status") or "").lower() if isinstance(report, dict) else ""
        succeeded = isinstance(report, dict) and (report.get("succeeded") is True or status in ("succeeded", "success"))
        if succeeded and not args.get("skip_launch"):
            startup = await run("hf_check_packaged_startup", {"timeout_seconds": float(args.get("timeout_seconds") or 120)})
            out["startup"] = startup
            started_ok = isinstance(startup, dict) and (startup.get("succeeded") is True or str(startup.get("status", "")).lower() in ("ok", "passed", "succeeded"))
            out["verdict"] = "ships: packaged and started" if started_ok else "packaged, but the build did not start cleanly"
        else:
            out["verdict"] = "packaging still running or failed: see package_report (first error is listed there)"
        return out
    raise DesignError("mode is check, package or report.")


# ---------------------------------------------------------- 2 perf budget

DEFAULT_BUDGET = {"frame_ms": 16.6, "frame_p95_ms": 20.0, "game_ms": 8.0, "render_ms": 8.0, "gpu_ms": 14.0,
                  "hitches_per_minute": 2}
# Where each budget lives in an hf_get_profile_run result.
_BUDGET_PATHS = {"frame_ms": ("ms", "frame", "avg"), "frame_p95_ms": ("ms", "frame", "p95"),
                 "game_ms": ("ms", "game", "avg"), "render_ms": ("ms", "render", "avg"),
                 "gpu_ms": ("ms", "gpu", "avg"), "hitches_per_minute": ("hitches", "per_minute")}


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    for k in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def compare_budget(result: dict[str, Any], budget: dict[str, float]) -> tuple[list, list, list]:
    over, within, unknown = [], [], []
    for key, limit in budget.items():
        path = _BUDGET_PATHS.get(key)
        v = _dig(result, path) if path else None
        if not isinstance(v, (int, float)):
            unknown.append(key)
            continue
        (over if v > float(limit) else within).append({"budget": key, "limit": limit, "measured": round(float(v), 2)})
    return over, within, unknown


async def _pie_playing(run: Run) -> bool:
    st = await run("hf_get_pie_state", {})
    return isinstance(st, dict) and bool(st.get("playing"))


async def perf_budget(run: Run, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    budget = {**DEFAULT_BUDGET, **(args.get("budget") or {})}
    unknown_keys = [k for k in budget if k not in _BUDGET_PATHS]
    if unknown_keys:
        raise DesignError(f"budget keys are {sorted(_BUDGET_PATHS)}; not {unknown_keys}.")
    seconds = float(args.get("seconds") or 20)
    prof_args: dict[str, Any] = {"seconds": seconds, "warmup_seconds": 2}
    started_pie = False
    if args.get("map"):
        prof_args.update(target="standalone", map=str(args["map"]))
    elif not await _pie_playing(run):
        await run("hf_start_pie", {})
        started_pie = True
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not await _pie_playing(run):
            await asyncio.sleep(1.0)
    try:
        started = await run("hf_profile_run", prof_args)
        result = None
        if isinstance(started, dict) and started.get("run_id") is not None:
            deadline = time.monotonic() + seconds + (240 if args.get("map") else 60)
            while time.monotonic() < deadline:
                await asyncio.sleep(2.0)
                r = await run("hf_get_profile_run", {"run_id": started["run_id"]})
                if isinstance(r, dict) and r.get("done"):
                    result = r.get("result") or r
                    break
    finally:
        if started_pie:
            await run("hf_stop_pie", {})
    over, within, unknown = compare_budget(result or {}, budget)
    cost = await run("hf_audit_scene_cost", {"top": 12})
    tex = await run("hf_audit_textures", {"large_texture_size": int(args.get("large_texture") or 2048), "limit": 15})
    lods = await run("hf_audit_lods", {"limit": 15})
    bound = (result or {}).get("bound_by") if isinstance(result, dict) else None
    suggestions = []
    names = {o["budget"] for o in over}
    if names & {"gpu_ms", "render_ms"}:
        suggestions.append("GPU or render bound: start with the heaviest meshes in scene_cost (Nanite on high-poly "
                           "static meshes, LODs where lod_issues flags them, fewer dynamic shadow casters).")
    if "game_ms" in names or (isinstance(bound, dict) and (bound.get("game_pct") or 0) >= 60 and over):
        suggestions.append("Game thread bound: look for ticking actors (the health report lists empty Tick events) "
                           "and per-frame Blueprint work; hf_profile_run's worst_frames show when it spikes.")
    if "hitches_per_minute" in names or "frame_p95_ms" in names:
        suggestions.append("Spikes: usually streaming, garbage collection or shader compiles; check large_textures "
                           "and PSO caching, and compare worst_frames with what happened at that moment.")
    if findings_of(tex)[0]:
        suggestions.append("Textures over the size limit cost memory and streaming: lower Max Texture Size or LOD bias.")
    out = {"verdict": ("over budget: " + ", ".join(sorted(names))) if over else ("within budget" if within else "could not measure"),
           "over_budget": over, "within_budget": within, "not_measured": unknown,
           "fps_avg": (result or {}).get("fps_avg") if isinstance(result, dict) else None, "bound_by": bound,
           "worst_frames": ((result or {}).get("worst_frames") or [])[:5] if isinstance(result, dict) else [],
           "top_scene_costs": findings_of(cost, 12)[1] if findings_of(cost)[0] else cost,
           "large_textures": findings_of(tex, 10), "lod_issues": findings_of(lods, 10), "suggestions": suggestions}
    if not args.get("map"):
        out["note"] = ("Measured in Play In Editor, where the editor's own work lands on the game thread and inflates "
                       "game_ms. Pass 'map' to measure a standalone game before acting on a game-thread overrun.")
    baseline = args.get("baseline")
    if baseline:
        name = safe_id(baseline, "baseline")
        if args.get("save_baseline"):
            out["baseline_saved"] = await run("hf_save_perf_baseline", {"name": name, "note": "hf_perf_budget"})
        else:
            out["vs_baseline"] = await run("hf_compare_perf_baseline", {"top": 10})
    store.save({"budget": budget, "over": over, "within": within, "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
               "perf", f"budget-{time.strftime('%Y%m%d-%H%M%S')}.json")
    return out


# --------------------------------------------------------- 3 accessibility

A11Y_SETTINGS = [
    ("Subtitles", "bool", "On by default for any spoken line."),
    ("SubtitleSize", "float", "Scale, 1 = default; offer at least 1 to 2."),
    ("SubtitleBackground", "float", "Opacity of the box behind subtitles, 0 to 1."),
    ("TextScale", "float", "UI text scale."),
    ("ColorblindMode", "name", "Off, Protanopia, Deuteranopia, Tritanopia."),
    ("ScreenShakeScale", "float", "0 turns shake off."),
    ("CameraBobScale", "float", "Head bob and sway, 0 turns it off."),
    ("HoldToToggle", "bool", "Hold actions can be toggles."),
    ("AimAssist", "float", "0 off to 1 full."),
    ("MotionBlur", "bool", "Off by default helps motion sickness."),
    ("FieldOfView", "float", "Wider FOV reduces motion sickness."),
    ("FlashReduction", "bool", "Tone down full-screen flashes."),
]


async def accessibility(run: Run, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    contexts = await run("hf_find_assets", {"path": "/Game", "recursive": True, "class_name": "InputMappingContext", "limit": 20})
    mappable, total, per = 0, 0, []
    for a in (contexts or {}).get("assets", []) if isinstance(contexts, dict) else []:
        r = await run("hf_describe_player_mappable_config", {"context": a.get("object_path")})
        if isinstance(r, dict):
            mappable += int(r.get("player_mappable_count") or 0)
            total += int(r.get("mapping_count") or 0)
            per.append(f"{a.get('name')}: {r.get('player_mappable_count', 0)} of {r.get('mapping_count', 0)}")
    checks.append({"check": "Controls can be remapped", "status": "pass" if mappable else ("fail" if total else "unknown"),
                   "detail": ("Mappable mappings per context: " + "; ".join(per)) if mappable else
                   ("No key mapping carries Player Mappable Key Settings, so a rebinding menu would be empty. "
                    "Add them to each Input Action (or mapping) in your mapping contexts." if total else
                    "No Enhanced Input mapping contexts were found.")})
    input_audit = await run("hf_audit_input", {"limit": 20})
    n, first = findings_of(input_audit, 8)
    checks.append({"check": "Input set has no conflicts or dead actions", "status": "pass" if n == 0 else "warn", "detail": first})
    audio = await run("hf_audit_audio", {"limit": 30})
    a_text = json.dumps(audio or {}, default=str).lower()
    checks.append({"check": "Dialogue sounds carry subtitles", "status": "warn" if "subtitle" in a_text else "unknown",
                   "detail": "See the audio audit's subtitle findings." if "subtitle" in a_text else
                   "Not detectable here: make sure every Dialogue Wave or voiced Sound Wave has subtitle text."})
    manual = ["Colour is never the only signal (pair colours with shapes, icons or text).",
              "UI text contrast at least 4.5:1 against its background.",
              "No information only by sound: every important cue has a visual one.",
              "No timed input the player cannot slow down or turn off.",
              "Pause anywhere, including cutscenes.",
              "Subtitles are on by default and can be resized, with a background box."]
    out: dict[str, Any] = {"checks": checks, "check_by_hand": manual}
    if args.get("scaffold_settings", True) is not False and run.call is not None:
        rows = [{"Name": n, "Kind": t, "Notes": note, "DefaultValue": ("True" if n in ("Subtitles", "FlashReduction") else
                 "1" if t == "float" else "False" if t == "bool" else "Off")} for n, t, note in A11Y_SETTINGS]
        cols = [("Kind", "name"), ("DefaultValue", "string"), ("Notes", "string")]
        try:
            out["settings_table"] = await write_table(run.call, str(args.get("folder") or "/Game/Design"), "S_AccessibilitySetting",
                                                      "DT_AccessibilitySettings", cols, rows, True)
            out["settings_note"] = ("A table of the settings a player expects, with defaults. Wire each to your options "
                                    "menu and save it with the save system (hf_generate_save_system).")
        except DesignError as exc:
            out["settings_table_error"] = str(exc)
    fails = [c for c in checks if c["status"] == "fail"]
    out["verdict"] = f"{len(fails)} failing check(s)" if fails else "no automatic failures; finish the hand checks"
    store.save(out, "accessibility.json")
    return out


# ---------------------------------------------------------- 4 localization

_EXPANSION = {"de": 1.35, "fr": 1.3, "es": 1.3, "ru": 1.3, "pt": 1.3, "it": 1.3, "pl": 1.35, "fi": 1.4}


def pseudo(text: str) -> str:
    """An accented, 40% longer stand-in that shows clipped and hard-coded text."""
    table = str.maketrans("aeiouAEIOUcnsy", "àéîõüÀÉÎÕÜçñšý")
    return "[" + text.translate(table) + " ~" * max(1, len(text) // 5) + "]"


async def localization(run: Run, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    audit = await run("hf_audit_localization", {"limit": 30})
    untranslated = await run("hf_list_untranslated_text", {})
    texts: list[str] = []
    for kind in ("dialogue", "barks", "quests"):
        folder = store.path(kind)
        for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            data = json.loads(f.read_text(encoding="utf-8"))
            if kind == "dialogue":
                for n in data.get("nodes", []):
                    texts.append(str(n.get("text") or ""))
                    texts += [str(c.get("text") or "") for c in n.get("choices") or []]
            elif kind == "barks":
                texts += [str(b.get("text") or "") for b in data.get("barks", [])]
            else:
                texts += [str(s.get("objective") or "") for s in data.get("steps", [])]
    texts = [t for t in texts if t.strip()]
    limit = int(args.get("max_chars") or 90)
    langs = _text_list(args.get("languages")) or list(_EXPANSION)
    risky = []
    for t in texts:
        worst = max(_EXPANSION.get(lang[:2], 1.3) for lang in langs)
        if len(t) * worst > limit:
            risky.append({"text": t[:80], "chars": len(t), "after_translation_about": int(len(t) * worst)})
    non_ascii = sorted({c for t in texts for c in t if ord(c) > 127})
    out = {"audit": audit, "untranslated": untranslated, "design_text_lines": len(texts),
           "may_not_fit": risky[:30], "may_not_fit_count": len(risky),
           "pseudo_examples": [pseudo(t) for t in texts[:5]],
           "characters_beyond_ascii": "".join(non_ascii[:80]),
           "font_note": "Check the UI fonts include every character of each target language (CJK needs its own font)."}
    target = args.get("target")
    if target:
        out["config"] = await run("hf_write_localization_config", {"target": str(target)})
    out["verdict"] = (f"{len(risky)} line(s) may not fit after translation" if risky else "design text fits the length limit") + \
                     ("; see untranslated" if untranslated else "")
    return out


# ------------------------------------------------------------- 5 tutorial

VERB_ORDER = ["move", "look", "jump", "crouch", "sprint", "interact", "use", "pick", "attack", "fire", "shoot", "aim",
              "reload", "dodge", "block", "ability", "inventory", "map", "pause"]


def order_actions(actions: list[str]) -> list[str]:
    def rank(a: str) -> tuple[int, str]:
        low = a.lower()
        for i, v in enumerate(VERB_ORDER):
            if v in low:
                return i, low
        return len(VERB_ORDER), low
    return sorted(dict.fromkeys(actions), key=rank)


def action_names(mappings: Any) -> list[str]:
    names: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k in ("action", "name", "action_name"):
                v = o.get(k)
                if isinstance(v, str) and (v.startswith("IA_") or "/" in v or k == "action"):
                    names.append(v.rsplit(".", 1)[-1].rsplit("/", 1)[-1])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(mappings)
    return [n for n in dict.fromkeys(names) if n]


async def tutorial(run: Run, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    steps_in = args.get("steps")
    if not steps_in:
        found = await run("hf_find_assets", {"path": "/Game", "recursive": True, "class_name": "InputAction", "limit": 60})
        names = [a.get("name") for a in (found or {}).get("assets", [])] if isinstance(found, dict) else []
        if not names:
            names = action_names(await run("hf_describe_input_mappings", {"include_names": True}))
        acts = order_actions([n for n in names if n])
        if not acts:
            raise DesignError("no input actions were found; give 'steps' [{action, prompt, done_when}].")
        steps_in = [{"action": a, "prompt": f"Press the {a.removeprefix('IA_')} key",
                     "done_when": f"the player has used {a}"} for a in acts[:10]]
    rows = []
    bot = []
    for i, s in enumerate(steps_in):
        act = str(s.get("action") or "")
        if not act:
            raise DesignError(f"tutorial step {i + 1} needs an 'action'.")
        rows.append({"Name": f"Tutorial.{i + 1:02d}", "Action": act, "Prompt": str(s.get("prompt") or ""),
                     "DoneWhen": str(s.get("done_when") or ""), "Order": i + 1})
        bot += [{"type": "input", "name": act, "value": 1.0, "seconds": 0.5}, {"type": "wait", "seconds": 0.5}]
    out: dict[str, Any] = {"steps": rows}
    out["table"] = await write_table(run.call, str(args.get("folder") or "/Game/Design"), "S_TutorialStep", "DT_Tutorial",
                                     [("Action", "name"), ("Prompt", "text"), ("DoneWhen", "string"), ("Order", "int")], rows, True)
    out["bot_script"] = bot
    out["next"] = ("Show each row's Prompt until its Action fires, then advance (a widget and a small manager reading "
                   "DT_Tutorial). Check the flow with hf_playtest_batch using bot_script plus your expect steps.")
    store.save({"steps": rows, "bot": bot}, "tutorial.json")
    return out


# ------------------------------------------------------------ 6 save game

_SAVE_TYPES = {"bool", "int", "float", "string", "name", "text", "vector", "rotator", "transform"}


async def save_system(run: Run, args: dict[str, Any]) -> dict[str, Any]:
    fields = args.get("fields") or []
    if not fields:
        raise DesignError("'fields' is [{name, type}] for everything the game must remember.")
    folder = "/" + str(args.get("folder") or "/Game/Design").strip("/")
    name = safe_id(args.get("name") or "BP_GameSave", "name")
    bp = f"{folder}/{name}"
    created = await run("hf_create_blueprint", {"parent_class": "/Script/Engine.SaveGame", "destination": bp, "compile": False})
    if created is None:
        raise DesignError(f"could not create {bp}: " + json.dumps(run.ran[-1]))
    added, failed = [], []
    for f in fields:
        fname = safe_id(f.get("name"), "field name")
        ftype = str(f.get("type") or "").lower()
        if ftype not in _SAVE_TYPES and not ftype.startswith(("array:", "map:", "set:", "struct:", "object:", "softobject:")):
            failed.append({"field": fname, "error": f"type is one of {sorted(_SAVE_TYPES)} (or array:/struct:...)"})
            continue
        r = await run("hf_add_blueprint_variable", {"blueprint": bp, "name": fname, "type": ftype,
                                                    **({"default_value": str(f["default"])} if "default" in f else {})})
        (added if r is not None else failed).append(fname if r is not None else {"field": fname, "error": run.ran[-1].get("error")})
    compiled = await run("hf_compile_blueprint", {"blueprint": bp})
    slot = str(args.get("slot") or "Slot1")
    return {"save_game": bp, "fields": added, "failed": failed, "compiled": compiled,
            "use": [f"Save: Create Save Game Object ({name}), set its fields, Save Game to Slot ('{slot}', 0).",
                    f"Load: Does Save Game Exist ('{slot}', 0) then Load Game from Slot, Cast to {name}, read the fields.",
                    "Test: set a value in PIE, save, stop PIE, start again, load, and check it (hf_pie_run_playtest)."]}


# ---------------------------------------------------------------- 7 juice

JUICE = [("HitStopSeconds", 0.06, "Freeze on a solid hit (0 = off)."), ("HitStopTimeScale", 0.05, "Time scale during hit stop."),
         ("ShakeScale", 1.0, "Camera shake multiplier (0 = off)."), ("ImpactParticleScale", 1.0, "Hit effect size."),
         ("DamageNumbers", 1.0, "1 shows floating damage numbers."), ("DamageNumberScale", 1.0, "Their size."),
         ("FlashOnHit", 1.0, "1 flashes the hit actor's material."), ("KnockbackScale", 1.0, "Hit push multiplier."),
         ("HitSoundVolume", 1.0, "Impact sound volume."), ("FovKick", 2.0, "Degrees of FOV punch on a big hit.")]


async def juice(run: Run, args: dict[str, Any]) -> dict[str, Any]:
    overrides = args.get("values") or {}
    rows = [{"Name": n, "Value": float(overrides.get(n, v)), "Notes": note} for n, v, note in JUICE]
    table = await write_table(run.call, str(args.get("folder") or "/Game/Design"), "S_JuiceSetting", "DT_Juice",
                              [("Value", "float"), ("Notes", "string")], rows, True)
    out = {"table": table, "values": {r["Name"]: r["Value"] for r in rows},
           "wire": ["On hit: Set Global Time Dilation to HitStopTimeScale, wait HitStopSeconds (real time), restore 1.",
                    "Camera shake: Start Camera Shake with Scale = ShakeScale (0 skips it); respect the accessibility ShakeScale too.",
                    "Damage numbers: spawn a widget component showing the damage when DamageNumbers is 1.",
                    "Read each value with Get Data Table Row (DT_Juice, name)."]}
    if args.get("preview_actor"):
        out["preview_time_dilation"] = await run("hf_set_custom_time_dilation", {"component": str(args["preview_actor"]),
                                                                                 "value": rows[1]["Value"]})
    return out


# --------------------------------------------------------------- 8 store kit

STORE_SIZES = {
    "steam": {"header_capsule": (920, 430), "small_capsule": (462, 174), "main_capsule": (1232, 706),
              "vertical_capsule": (748, 896), "library_capsule": (600, 900), "library_hero": (3840, 1240),
              "page_background": (1438, 810), "screenshot": (1920, 1080)},
    "itch": {"cover": (630, 500), "screenshot": (1920, 1080)},
    "epic": {"landscape": (2560, 1440), "portrait": (1200, 1600), "screenshot": (1920, 1080)},
}


async def store_kit(run: Run, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    stores = _text_list(args.get("stores")) or ["steam"]
    sizes = {f"{s}.{k}": v for s in stores for k, v in STORE_SIZES.get(s, {}).items()}
    if not sizes:
        raise DesignError(f"stores are {sorted(STORE_SIZES)}.")
    shots = args.get("shots") or []
    taken = []
    biggest = max(max(w, h) for w, h in sizes.values())
    for i, shot in enumerate(shots[:12]):
        loc, rot = shot.get("location"), shot.get("rotation")
        if loc and rot:
            await run("hf_set_camera", {"location": str(loc), "rotation": str(rot)})
        res = f"{min(7680, max(1920, biggest))}x{int(min(7680, max(1920, biggest)) * 9 / 16)}"
        r = await run("hf_run_console_command", {"name": f"HighResShot {res}"})
        taken.append({"shot": shot.get("name") or f"shot{i + 1}", "resolution": res, "ok": r is not None})
    doc = store.load("design.json") or {}
    plan = {"sizes": {k: f"{w}x{h}" for k, (w, h) in sizes.items()},
            "shots": taken,
            "trailer_shot_list": [
                "0-5 s: the hook, the most striking moment of play, no logo first.",
                "5-20 s: the core loop in three quick beats" + (f" ({doc.get('core_loop')})" if doc.get("core_loop") else "") + ".",
                "20-40 s: variety: places, enemies, tools.",
                "40-55 s: the escalation, the biggest moment.",
                "55-60 s: title, platforms, wishlist call."],
            "description_inputs": {k: doc.get(k) for k in ("title", "pitch", "pillars", "features") if doc.get(k)},
            "next": ("HighResShot images are in Saved/Screenshots/WindowsEditor. Crop each to the sizes above with the "
                     "key art inside the safe area; titles and logos are added by hand. Draft the description from "
                     "description_inputs and review it: nothing is published from here.")}
    store.save(plan, "store_kit.json")
    return plan


# ---------------------------------------------------------- 9 telemetry


_HFT = re.compile(r"HFT\|([A-Za-z0-9_.-]+)\|?(.*)$")


def parse_telemetry(lines: list[str]) -> dict[str, Any]:
    """Lines 'HFT|event|key=value|key=value' from any log: deaths, sections, quits."""
    events: list[dict[str, Any]] = []
    for line in lines:
        m = _HFT.search(line.strip())
        if not m:
            continue
        ev: dict[str, Any] = {"event": m.group(1)}
        for part in m.group(2).split("|"):
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    ev[k] = float(v)
                except ValueError:
                    ev[k] = v
        events.append(ev)
    by_section: dict[str, dict[str, Any]] = {}
    for ev in events:
        sec = str(ev.get("section") or "")
        if not sec:
            continue
        s = by_section.setdefault(sec, {"enter": 0, "deaths": 0, "complete": 0, "quit": 0, "seconds": []})
        e = ev["event"].lower()
        if e in ("enter", "section_enter"):
            s["enter"] += 1
        elif e in ("death", "died"):
            s["deaths"] += 1
        elif e in ("complete", "section_complete", "clear"):
            s["complete"] += 1
            if isinstance(ev.get("t"), float):
                s["seconds"].append(ev["t"])
        elif e in ("quit", "exit"):
            s["quit"] += 1
    for s in by_section.values():
        xs = sorted(s.pop("seconds"))
        s["median_seconds"] = xs[len(xs) // 2] if xs else None
        s["deaths_per_attempt"] = round(s["deaths"] / s["enter"], 2) if s["enter"] else None
    return {"events": len(events), "sections": by_section}


async def telemetry(store: Store, args: dict[str, Any]) -> dict[str, Any]:
    from pathlib import Path
    logs = Path(store.root).parent.parent / "Saved" / "Logs"
    files = [Path(p) for p in _text_list(args.get("files"))] or (sorted(logs.glob("*.log"))[-20:] if logs.is_dir() else [])
    lines: list[str] = []
    for f in files:
        try:
            lines += f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
    out = parse_telemetry(lines)
    out["files"] = [str(f) for f in files][:20]
    curve = args.get("predicted")  # [{section, difficulty}] from hf_balance_sim mode difficulty
    if isinstance(curve, list):
        cmp = []
        for row in curve:
            sec = str(row.get("section"))
            real = out["sections"].get(sec, {})
            cmp.append({"section": sec, "predicted_difficulty": row.get("difficulty"),
                        "deaths_per_attempt": real.get("deaths_per_attempt"), "quit": real.get("quit")})
        out["vs_prediction"] = cmp
    out["how_to_log"] = ("From the game (development builds), print a log line per event: "
                         "'HFT|enter|section=Ambush', 'HFT|death|section=Ambush|cause=Drone', "
                         "'HFT|complete|section=Ambush|t=42.5', 'HFT|quit|section=Ambush'. Print String with "
                         "'Print to Log' on is enough; testers send their Saved/Logs folder.")
    if not out["events"]:
        out["verdict"] = "no HFT lines found yet: add the log lines below, play, then run this again"
    else:
        worst = sorted(((k, v.get("deaths_per_attempt") or 0) for k, v in out["sections"].items()), key=lambda kv: -kv[1])[:3]
        out["verdict"] = "hardest sections by deaths per attempt: " + ", ".join(f"{k} ({v})" for k, v in worst)
    return out


# ------------------------------------------------------------ 10 health


async def health(run: Run, store: Store, args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "/Game")
    sections = {
        "assets": await run("hf_audit_assets", {"path": path, "limit": 25}),
        "blueprints": await run("hf_audit_blueprints", {"path": path, "limit": 25}),
        "redirectors": await run("hf_find_redirectors", {"path": path, "limit": 25}),
        "textures": await run("hf_audit_textures", {"path": path, "limit": 25}),
        "lods": await run("hf_audit_lods", {"path": path, "limit": 25}),
        "uncooked_references": await run("hf_find_uncooked_references", {"limit": 25, "hard_only": True}),
        "data_tables": await run("hf_audit_data_tables", {}),
    }
    weight = {"error": 3, "critical": 3, "blocker": 3, "warning": 2, "warn": 2, "info": 0, "note": 0}
    actions = []
    for k, v in sections.items():
        n, first = findings_of(v, 200)
        if not n:
            continue
        sev = [str(f.get("severity", "warning")).lower() if isinstance(f, dict) else "warning" for f in first]
        score = max((weight.get(x, 1) for x in sev), default=1)
        real = [f for f, x in zip(first, sev) if weight.get(x, 1) > 0]
        actions.append({"area": k, "count": n, "worst": max(sev, key=lambda x: weight.get(x, 1)) if sev else "unknown",
                        "first": (real or first)[:3], "_score": score})
    doc = store.load("design.json")
    drift = None
    if doc:
        overview = await run("hf_get_project_overview", {"top": 20})
        text = json.dumps(overview or {}, default=str).lower()
        missing = [f["name"] for f in doc.get("features") or [] if f.get("priority") == "must"
                   and not any(w in text for w in re.findall(r"[a-z]{4,}", f["name"].lower()))]
        drift = {"must_features_with_nothing_in_project": missing, "doc_findings": doc_findings(doc)}
    actions.sort(key=lambda a: (-a.pop("_score"), -a["count"]))
    out = {"actions": actions, "design_drift": drift,
           "ran": run.ran, "verdict": f"{sum(a['count'] for a in actions)} item(s) across {len(actions)} area(s)"}
    store.save({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "actions": out["actions"], "design_drift": drift},
               "health", f"health-{time.strftime('%Y%m%d')}.json")
    return out


# ------------------------------------------------------- 11 multiplayer


async def multiplayer(run: Run, args: dict[str, Any]) -> dict[str, Any]:
    repl = await run("hf_audit_replication", {"limit": 40})
    rpc = await run("hf_audit_rpc_payloads", {"limit": 40})
    out: dict[str, Any] = {"replication": findings_of(repl, 15), "rpc_payloads": findings_of(rpc, 15)}
    checks = args.get("checks") or []
    if checks:
        started = await run("hf_start_pie", {"net_mode": "listen_server", "clients": max(2, int(args.get("clients") or 2))})
        out["pie"] = started
        results = []
        for c in checks[:20]:
            r = await run("hf_pie_check_replication", {"actor": str(c.get("actor")), "property": str(c.get("property"))})
            results.append({"actor": c.get("actor"), "property": c.get("property"), "result": r})
        out["replication_checks"] = results
        await run("hf_stop_pie", {})
    n = out["replication"][0] + out["rpc_payloads"][0]
    out["verdict"] = f"{n} replication finding(s)" + ("" if checks else "; give 'checks' [{actor, property}] for a live two-player test")
    return out


# ------------------------------------------------------------------- tools

TOOLS: list[ToolDef] = [
    ToolDef("hf_ship_check", WRITE,
            "Will it ship? mode check (default): shipping settings, cook readiness, references the cook would miss, "
            "and the estimated package size, with a ready/blocked verdict. mode package: the same, then starts "
            "packaging for the platform in the background. mode report: reads the packaging log (first error) and, "
            "if it packaged, launches the build and checks it starts.",
            _obj({"mode": {"type": "string", "enum": ["check", "package", "report"], "default": "check"},
                  "platform": {"type": "string", "default": "Win64"}, "maps": {"type": "array", "items": _S},
                  "package_anyway": {"type": "boolean", "default": False}, "skip_launch": {"type": "boolean", "default": False},
                  "timeout_seconds": {"type": "number", "default": 120}})),
    ToolDef("hf_perf_budget", WRITE,
            "Is it fast enough? Profiles a play session (PIE) and compares it with budgets (frame, game, draw and "
            "p95 frame, game, render and GPU ms, hitches per minute; defaults aim at 60 fps), starting and "
            "stopping Play In Editor if needed ('map' profiles a standalone game instead), then names the worst "
            "offenders from the scene cost, texture and LOD audits with what to try. 'baseline' with save_baseline stores the "
            "numbers; later calls with the same baseline report what changed.",
            _obj({"budget": {"type": "object", "description": "Overrides, e.g. {frame_ms: 33.3} for 30 fps."},
                  "seconds": {"type": "number", "default": 20}, "map": _S, "baseline": _S,
                  "save_baseline": {"type": "boolean", "default": False}, "large_texture": {"type": "integer", "default": 2048}})),
    ToolDef("hf_accessibility_pass", WRITE,
            "Check the game against common accessibility needs (remappable controls in the Enhanced Input "
            "mapping contexts, input conflicts, voiced audio with subtitles) and list the checks only a person can make. Unless "
            "scaffold_settings is false, also writes DT_AccessibilitySettings: subtitles, sizes, colour-blind "
            "mode, shake and bob scale, hold-to-toggle, aim assist, motion blur, FOV, flash reduction, with defaults.",
            _obj({"scaffold_settings": {"type": "boolean", "default": True}, "folder": _S})),
    ToolDef("hf_localization_pass", READ,
            "Get ready to translate: the project's localization audit and untranslated text, every player-facing "
            "line in the Design folder, lines likely too long once translated (German and Finnish run about "
            "35-40% longer), pseudo-localized examples, and characters fonts must cover. With 'target', writes "
            "the localization config for that target.",
            _obj({"languages": {"type": "array", "items": _S}, "max_chars": {"type": "integer", "default": 90}, "target": _S})),
    ToolDef("hf_build_tutorial", WRITE,
            "Plan the first minutes: from 'steps' [{action, prompt, done_when}] or, if none, from the project's "
            "input actions in teaching order (move, look, jump, interact, attack...), writes DT_Tutorial and "
            "returns a playtest bot script that uses each action, for hf_playtest_batch.",
            _obj({"steps": {"type": "array", "items": {"type": "object"}}, "folder": _S})),
    ToolDef("hf_generate_save_system", WRITE,
            "Make a SaveGame Blueprint holding what the game must remember ('fields' [{name, type, default}], "
            "types bool, int, float, string, name, text, vector, rotator, transform or array:/struct:...), "
            "compile it, and say how to save, load and test it.",
            _obj({"fields": {"type": "array", "items": {"type": "object"}}, "name": _S, "folder": _S, "slot": _S}, ["fields"])),
    ToolDef("hf_juice_kit", WRITE,
            "Make actions feel good, tunably: writes DT_Juice (hit stop length and time scale, shake, impact size, "
            "damage numbers, hit flash, knockback, hit volume, FOV kick) with defaults or 'values', and how to "
            "wire each. With preview_actor, applies the hit-stop time scale to that actor so you can see it in PIE.",
            _obj({"values": {"type": "object"}, "folder": _S, "preview_actor": _S})),
    ToolDef("hf_store_kit", WRITE,
            "Store page preparation: every required capsule and screenshot size for the stores (steam, itch, "
            "epic), high-resolution shots from the 'shots' camera positions [{name, location, rotation}], a "
            "trailer shot list, and the design doc's pitch and features as description inputs. Nothing is "
            "published.",
            _obj({"stores": {"type": "array", "items": _S}, "shots": {"type": "array", "items": {"type": "object"}}})),
    ToolDef("hf_playtest_feedback", READ,
            "Read real testers' sessions: 'HFT|event|key=value' lines from the project's logs (or 'files'): entries, "
            "deaths, completions and quits per section, deaths per attempt, median time, and, with 'predicted' "
            "(hf_balance_sim difficulty rows), predicted against real. Says how to emit the lines.",
            _obj({"files": {"type": "array", "items": _S}, "predicted": {"type": "array", "items": {"type": "object"}}})),
    ToolDef("hf_health_report", READ,
            "What to fix this week: asset, Blueprint, redirector, texture, LOD, data table and cook-reference "
            "audits in one ranked list, plus design drift ('must' features in the design doc with nothing in the "
            "project). Saved by date in HullForge/Design/health.",
            _obj({"path": {"type": "string", "default": "/Game"}})),
    ToolDef("hf_multiplayer_check", WRITE,
            "Multiplayer readiness: replication and RPC payload audits, and with 'checks' [{actor, property}] a "
            "listen-server PIE with two or more players that checks each property replicates.",
            _obj({"checks": {"type": "array", "items": {"type": "object"}}, "clients": {"type": "integer", "default": 2}})),
]
TOOL_NAMES = frozenset(t.name for t in TOOLS)


async def call_tool(name: str, args: dict[str, Any], store: Store, engine: EngineCall | None) -> dict[str, Any]:
    if name == "hf_playtest_feedback":
        return await telemetry(store, args)
    if engine is None:
        raise DesignError(f"{name} needs the editor connected.")
    run = Run(engine)
    if name == "hf_ship_check":
        out = await ship_check(run, args)
    elif name == "hf_perf_budget":
        out = await perf_budget(run, store, args)
    elif name == "hf_accessibility_pass":
        out = await accessibility(run, store, args)
    elif name == "hf_localization_pass":
        out = await localization(run, store, args)
    elif name == "hf_build_tutorial":
        out = await tutorial(run, store, args)
    elif name == "hf_generate_save_system":
        out = await save_system(run, args)
    elif name == "hf_juice_kit":
        out = await juice(run, args)
    elif name == "hf_store_kit":
        out = await store_kit(run, store, args)
    elif name == "hf_health_report":
        return await health(run, store, args)
    elif name == "hf_multiplayer_check":
        out = await multiplayer(run, args)
    else:
        raise DesignError(f"'{name}' is not a workflow tool.")
    failed = [r for r in run.ran if not r["ok"]]
    if failed:
        out["steps_that_failed"] = failed
    return out
