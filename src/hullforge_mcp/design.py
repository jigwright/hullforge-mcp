"""Game design tools, run by the connector rather than the editor.

These help someone get from a game idea to data the game runs on: a design
doc, a story bible, dialogue, barks, quests, tuning values, item and enemy
tables, and a balance simulator. The writing itself (the story, the lines,
the numbers) comes from the model and the person; what these tools add is
structure, checks, and getting the result into the project:

* everything is saved as JSON (and a readable Markdown copy) under
  <Project>/HullForge/Design, so it lives with the project, goes into source
  control, and every later step reads the same facts;
* checks that a model is bad at doing by eye: graph checks for dialogue and
  quests, names against the story bible, numbers against balance rules;
* the arithmetic of balance (time to kill, economy over time, level curves)
  done exactly instead of estimated;
* tables written into Data Tables the game reads, through the editor's own
  struct and Data Table tools, with row structs that belong to the project
  (no runtime dependency on HullForge).

Nothing here is paid, and nothing writes outside the Design folder except
through the editor's own tools.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable

AREA = "Game Design"
READ, WRITE = "read", "write"

# Called for every editor tool these use: (tool, args) -> (ok, result or error text).
EngineCall = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, Any]]]


class DesignError(Exception):
    """A refusal with a message for the model; nothing was changed."""


# --------------------------------------------------------------------- store


class Store:
    """The Design folder of one project."""

    def __init__(self, project_dir: str | Path) -> None:
        self.root = Path(project_dir) / "HullForge" / "Design"

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        # Every name that reaches a path is checked by safe_id, but keep the
        # folder a hard boundary anyway.
        if self.root.resolve() not in p.resolve().parents and p.resolve() != self.root.resolve():
            raise DesignError("that name would leave the Design folder.")
        return p

    def load(self, *parts: str, default: Any = None) -> Any:
        p = self.path(*parts)
        if not p.is_file():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DesignError(f"{p.name} could not be read ({exc}); fix or remove it.") from exc

    def save(self, data: Any, *parts: str, markdown: str | None = None, history: bool = False) -> Path:
        p = self.path(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        if history and p.is_file():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            h = self.path("history", f"{p.stem}-{stamp}.json")
            h.parent.mkdir(parents=True, exist_ok=True)
            h.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if markdown is not None:
            p.with_suffix(".md").write_text(markdown, encoding="utf-8")
        return p


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def safe_id(value: Any, what: str) -> str:
    s = str(value or "").strip()
    if not _ID.match(s) or ".." in s:
        raise DesignError(f"'{what}' must be 1 to 64 letters, digits, '_', '-' or '.', starting with a letter or digit.")
    return s


def _text_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x) for x in v if str(x).strip()]


# ---------------------------------------------------------------- design doc

DOC_SECTIONS = {
    "title": "The working title.",
    "pitch": "One or two sentences: what the player does and why it is fun.",
    "genre": "Genre and closest comparable games.",
    "audience": "Who it is for and on what platforms.",
    "pillars": "Three to five design pillars: the tests every feature must pass.",
    "core_loop": "The moment-to-moment loop, then the session loop, then the long-term loop.",
    "feel": "The target feel: pace, camera, controls, tone.",
    "features": "Features as objects {name, priority (must|should|could), pillar, notes}.",
    "scope": "What the first release contains: levels, hours of play, team, time.",
    "out_of_scope": "What it deliberately does not contain.",
    "risks": "The riskiest assumptions, to test first.",
    "references": "Games, films, art to point at.",
}


def merge_doc(doc: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    unknown = [k for k in changes if k not in DOC_SECTIONS]
    if unknown:
        raise DesignError(f"unknown section(s) {unknown}; the sections are {list(DOC_SECTIONS)}.")
    out = dict(doc)
    for k, v in changes.items():
        if v is None:
            out.pop(k, None)
        elif k == "features":
            out[k] = [_feature(f) for f in (v if isinstance(v, list) else [v])]
        elif k in ("pillars", "out_of_scope", "risks", "references"):
            out[k] = _text_list(v)
        else:
            out[k] = str(v)
    out["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return out


def _feature(f: Any) -> dict[str, Any]:
    if isinstance(f, str):
        f = {"name": f}
    if not isinstance(f, dict) or not str(f.get("name") or "").strip():
        raise DesignError("each feature needs a 'name'.")
    pr = str(f.get("priority") or "should").lower()
    if pr not in ("must", "should", "could"):
        raise DesignError(f"feature '{f['name']}': priority is must, should or could.")
    out = {"name": str(f["name"]), "priority": pr}
    for k in ("pillar", "notes"):
        if f.get(k):
            out[k] = str(f[k])
    if f.get("needs"):
        out["needs"] = _text_list(f["needs"])
    return out


def doc_markdown(doc: dict[str, Any]) -> str:
    lines = [f"# {doc.get('title') or 'Untitled game'}", ""]
    for k, about in DOC_SECTIONS.items():
        if k == "title" or k not in doc:
            continue
        lines += [f"## {k.replace('_', ' ').capitalize()}", ""]
        v = doc[k]
        if k == "features":
            for f in v:
                extra = f" ({f['pillar']})" if f.get("pillar") else ""
                lines.append(f"- **{f['name']}** [{f['priority']}]{extra}" + (f": {f['notes']}" if f.get("notes") else ""))
        elif isinstance(v, list):
            lines += [f"- {x}" for x in v]
        else:
            lines.append(str(v))
        lines.append("")
    lines.append(f"_Updated {doc.get('updated', '')}. Kept by HullForge in HullForge/Design/design.json._")
    return "\n".join(lines) + "\n"


def doc_findings(doc: dict[str, Any]) -> list[str]:
    f: list[str] = []
    for k in ("pitch", "pillars", "core_loop", "scope"):
        if not doc.get(k):
            f.append(f"No {k.replace('_', ' ')} yet.")
    pillars = doc.get("pillars") or []
    if pillars and not 3 <= len(pillars) <= 5:
        f.append(f"{len(pillars)} pillars: three to five keep them useful as tests.")
    feats = doc.get("features") or []
    names = {p.lower() for p in pillars}
    for ft in feats:
        if ft.get("pillar") and names and ft["pillar"].lower() not in names:
            f.append(f"Feature '{ft['name']}' names pillar '{ft['pillar']}', which is not one of the pillars.")
    if pillars and feats and not any(ft.get("pillar") for ft in feats):
        f.append("No feature says which pillar it serves.")
    musts = [ft for ft in feats if ft["priority"] == "must"]
    if feats and len(musts) > max(5, len(feats) * 0.6):
        f.append(f"{len(musts)} of {len(feats)} features are 'must': a slice needs a short must list.")
    out = {x.lower() for x in doc.get("out_of_scope") or []}
    for ft in feats:
        if ft["name"].lower() in out:
            f.append(f"Feature '{ft['name']}' is also listed as out of scope.")
    return f


def scope_report(doc: dict[str, Any], overview: Any) -> dict[str, Any]:
    feats = doc.get("features") or []
    slice_ = [ft for ft in feats if ft["priority"] == "must"]
    later = [ft for ft in feats if ft["priority"] != "must"]
    have = json.dumps(overview, default=str).lower() if overview else ""
    missing_needs = []
    for ft in feats:
        for need in ft.get("needs") or []:
            if have and need.lower() not in have:
                missing_needs.append({"feature": ft["name"], "needs": need})
    size = sum({"must": 3, "should": 2, "could": 1}[ft["priority"]] for ft in feats)
    return {
        "findings": doc_findings(doc),
        "vertical_slice": [ft["name"] for ft in slice_],
        "after_the_slice": [f"{ft['name']} [{ft['priority']}]" for ft in later],
        "not_found_in_project": missing_needs,
        "size": "small" if size <= 12 else "medium" if size <= 30 else "large",
        "size_note": "A rough guide from the number and priority of features, not a schedule: "
                     "test the riskiest 'must' first.",
        "project_overview": overview,
    }


# --------------------------------------------------------------- story bible

BIBLE_KINDS = ("character", "faction", "place", "item", "event", "term")


def bible_upsert(bible: dict[str, Any], entries: list[Any]) -> list[str]:
    done = []
    items = bible.setdefault("entries", {})
    for e in entries:
        if not isinstance(e, dict):
            raise DesignError("each entry is an object with kind and name.")
        kind = str(e.get("kind") or "").lower()
        name = str(e.get("name") or "").strip()
        if kind not in BIBLE_KINDS or not name:
            raise DesignError(f"each entry needs a name and a kind ({', '.join(BIBLE_KINDS)}).")
        key = name.lower()
        old = items.get(key, {})
        new = {"kind": kind, "name": name}
        for k in ("aliases", "facts"):
            new[k] = _text_list(e.get(k)) if k in e else old.get(k, [])
        for k in ("summary", "voice", "when"):
            if k in e or k in old:
                new[k] = str(e.get(k, old.get(k)) or "")
        if "relations" in e or "relations" in old:
            rel = e.get("relations", old.get("relations")) or {}
            if not isinstance(rel, dict):
                raise DesignError(f"'{name}': relations is an object, name to relation.")
            new["relations"] = {str(k): str(v) for k, v in rel.items()}
        items[key] = new
        done.append(name)
    bible["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return done


def bible_markdown(bible: dict[str, Any]) -> str:
    lines = ["# Story bible", ""]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for e in (bible.get("entries") or {}).values():
        by_kind.setdefault(e["kind"], []).append(e)
    for kind in BIBLE_KINDS:
        if kind not in by_kind:
            continue
        lines += [f"## {kind.capitalize()}s", ""]
        for e in sorted(by_kind[kind], key=lambda x: x["name"]):
            aka = f" (also {', '.join(e['aliases'])})" if e.get("aliases") else ""
            lines.append(f"### {e['name']}{aka}")
            for k in ("summary", "voice", "when"):
                if e.get(k):
                    lines.append(f"{k.capitalize()}: {e[k]}")
            lines += [f"- {x}" for x in e.get("facts") or []]
            for other, rel in (e.get("relations") or {}).items():
                lines.append(f"- {rel}: {other}")
            lines.append("")
    return "\n".join(lines) + "\n"


_PROPER = re.compile(r"\b([A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+)*)\b")
_COMMON = {
    "the", "a", "an", "i", "you", "he", "she", "it", "we", "they", "my", "your", "his", "her", "our", "their",
    "this", "that", "these", "those", "what", "who", "where", "when", "why", "how", "yes", "no", "not", "and",
    "but", "or", "if", "then", "so", "oh", "hey", "well", "now", "here", "there", "come", "go", "get", "let",
    "look", "listen", "wait", "stop", "run", "help", "thanks", "please", "sir", "madam", "lord", "lady",
    "okay", "ok", "hello", "goodbye", "good", "great", "do", "don't", "is", "are", "was", "be", "have", "can",
    "will", "would", "should", "could", "maybe", "never", "always", "all", "some", "one", "two", "three",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "god", "mister", "miss",
    "captain", "doctor", "sergeant", "king", "queen", "prince", "princess", "everyone", "nobody", "someone",
    "nothing", "something", "anything", "again", "just", "still", "even", "only", "too", "very", "more",
    "take", "give", "make", "keep", "tell", "find", "bring", "move", "hold", "careful", "quick", "quiet",
}


def bible_names(bible: dict[str, Any]) -> dict[str, str]:
    """Every name and alias (lowercase) -> the entry's name."""
    out: dict[str, str] = {}
    for e in (bible.get("entries") or {}).values():
        out[e["name"].lower()] = e["name"]
        for a in e.get("aliases") or []:
            out[a.lower()] = e["name"]
    return out


def check_text(bible: dict[str, Any], texts: list[str]) -> dict[str, Any]:
    names = bible_names(bible)
    known_words = {w for n in names for w in n.split()}
    mentioned: dict[str, int] = {}
    near: list[dict[str, Any]] = []
    unknown: dict[str, int] = {}
    for i, t in enumerate(texts):
        low = t.lower()
        for n, canon in names.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low):
                mentioned[canon] = mentioned.get(canon, 0) + 1
        for m in _PROPER.finditer(t):
            cand = m.group(1)
            words = cand.split()
            # Sentence-initial ordinary words are capitalised too.
            while words and words[0].lower() in _COMMON:
                words = words[1:]
            if not words:
                continue
            cand = " ".join(words)
            lc = cand.lower()
            # One capitalised word that opens a sentence is usually just a word.
            before = t[:m.start()].rstrip()
            if len(words) == 1 and m.group(1) == cand and (not before or before[-1] in ".!?\"'-:"):
                continue
            if lc in names or all(w.lower() in known_words for w in words):
                continue
            best, score = None, 0.0
            for n, canon in names.items():
                s = SequenceMatcher(None, lc, n).ratio()
                if s > score:
                    best, score = canon, s
            if best and score >= 0.8:
                near.append({"text_index": i, "found": cand, "did_you_mean": best, "similarity": round(score, 2)})
            else:
                unknown[cand] = unknown.get(cand, 0) + 1
    entries = bible.get("entries") or {}
    facts = {c: entries[c.lower()].get("facts", []) for c in mentioned if c.lower() in entries}
    return {
        "misspelled_names": near,
        "names_not_in_bible": sorted(unknown, key=lambda k: -unknown[k])[:40],
        "mentioned": mentioned,
        "facts_to_check": facts,
        "note": "Names are checked here; whether a line CONTRADICTS a fact is for you to judge "
                "against facts_to_check. A new name may be intended: add it to the bible if so.",
    }


# ------------------------------------------------------------------ dialogue


def check_dialogue(conv: dict[str, Any], bible: dict[str, Any] | None = None,
                   max_line_chars: int = 220) -> dict[str, Any]:
    nodes = conv.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise DesignError("a conversation needs 'nodes': a list of {id, speaker, text, next | choices | end}.")
    by_id: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for n in nodes:
        nid = str(n.get("id") or "")
        if not nid:
            problems.append("a node has no id.")
            continue
        if nid in by_id:
            problems.append(f"node id '{nid}' is used twice.")
        by_id[nid] = n
    start = str(conv.get("start") or (nodes[0].get("id") if nodes else ""))
    if start not in by_id:
        raise DesignError(f"start node '{start}' does not exist.")

    def targets(n: dict[str, Any]) -> list[str]:
        t = []
        if n.get("next"):
            t.append(str(n["next"]))
        for c in n.get("choices") or []:
            if c.get("next"):
                t.append(str(c["next"]))
        return t

    dead_ends, dangling, long_lines, empty = [], [], [], []
    for nid, n in by_id.items():
        if not str(n.get("text") or "").strip():
            empty.append(nid)
        if len(str(n.get("text") or "")) > max_line_chars:
            long_lines.append(nid)
        for c in n.get("choices") or []:
            if not str(c.get("text") or "").strip():
                problems.append(f"node '{nid}' has a choice with no text.")
            if not c.get("next") and not c.get("end"):
                problems.append(f"node '{nid}': choice '{c.get('text', '')}' leads nowhere (give next, or end: true).")
        for t in targets(n):
            if t not in by_id:
                dangling.append({"from": nid, "to": t})
        if not targets(n) and not n.get("end") and not any(c.get("end") for c in n.get("choices") or []):
            dead_ends.append(nid)
    seen, todo = set(), [start]
    while todo:
        x = todo.pop()
        if x in seen or x not in by_id:
            continue
        seen.add(x)
        todo += targets(by_id[x])
    unreachable = sorted(set(by_id) - seen)
    # A loop with no way out: nodes from which no end can be reached.
    can_end = {nid for nid, n in by_id.items()
               if n.get("end") or any(c.get("end") for c in n.get("choices") or [])}
    changed = True
    while changed:
        changed = False
        for nid, n in by_id.items():
            if nid not in can_end and any(t in can_end for t in targets(n)):
                can_end.add(nid)
                changed = True
    traps = sorted(nid for nid in seen if nid not in can_end and nid not in dead_ends)
    speakers = sorted({str(n.get("speaker") or "") for n in by_id.values() if n.get("speaker")})
    out: dict[str, Any] = {
        "id": conv.get("id"),
        "nodes": len(by_id),
        "speakers": speakers,
        "problems": problems,
        "dead_ends": dead_ends,
        "dangling_links": dangling,
        "unreachable": unreachable,
        "loops_without_exit": traps,
        "empty_lines": empty,
        "long_lines": long_lines,
    }
    if bible and bible.get("entries"):
        names = bible_names(bible)
        out["speakers_not_in_bible"] = [s for s in speakers if s.lower() not in names and s.lower() not in ("player", "narrator")]
        out["text_check"] = check_text(bible, [str(n.get("text") or "") for n in by_id.values()])
    out["ok"] = not (problems or dead_ends or dangling or unreachable or traps or empty)
    return out


def dialogue_rows(conv: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per node: a flat shape any Blueprint can read."""
    rows = []
    cid = str(conv["id"])
    for n in conv["nodes"]:
        choices = n.get("choices") or []
        row = {
            "Name": f"{cid}.{n['id']}",
            "Conversation": cid,
            "Node": str(n["id"]),
            "Speaker": str(n.get("speaker") or ""),
            "Line": str(n.get("text") or ""),
            "Next": f"{cid}.{n['next']}" if n.get("next") else "",
            "Condition": str(n.get("condition") or ""),
            "Sets": ";".join(_text_list(n.get("sets"))),
            "End": bool(n.get("end")),
        }
        for i in range(4):
            c = choices[i] if i < len(choices) else {}
            row[f"Choice{i + 1}"] = str(c.get("text") or "")
            row[f"Choice{i + 1}Next"] = f"{cid}.{c['next']}" if c.get("next") else ""
            row[f"Choice{i + 1}Condition"] = str(c.get("condition") or "")
        rows.append(row)
    return rows


DIALOGUE_COLUMNS = [("Conversation", "name"), ("Node", "name"), ("Speaker", "name"), ("Line", "text"),
                    ("Next", "name"), ("Condition", "string"), ("Sets", "string"), ("End", "bool")] + [
    (f"Choice{i}{s}", t) for i in range(1, 5) for s, t in (("", "text"), ("Next", "name"), ("Condition", "string"))]


# --------------------------------------------------------------------- barks


def check_barks(barks: list[dict[str, Any]], bible: dict[str, Any] | None) -> dict[str, Any]:
    seen: dict[str, int] = {}
    dupes, near = [], []
    norm = [re.sub(r"[^a-z0-9 ]", "", str(b.get("text") or "").lower()).strip() for b in barks]
    for i, t in enumerate(norm):
        if t in seen:
            dupes.append({"index": i, "same_as": seen[t]})
        else:
            seen[t] = i
    for i in range(len(norm)):
        for j in range(i + 1, min(len(norm), i + 200)):
            if norm[i] and norm[i] != norm[j] and SequenceMatcher(None, norm[i], norm[j]).ratio() > 0.9:
                near.append({"index": j, "like": i})
    out = {"barks": len(barks), "duplicates": dupes, "near_duplicates": near[:50],
           "missing_tags": [i for i, b in enumerate(barks) if not b.get("tag")]}
    if bible and bible.get("entries"):
        out["text_check"] = check_text(bible, [str(b.get("text") or "") for b in barks])
    return out


BARK_COLUMNS = [("Speaker", "name"), ("Tag", "name"), ("Line", "text"), ("Weight", "float"), ("Cooldown", "float")]


# -------------------------------------------------------------------- quests


def check_quest(q: dict[str, Any], known_items: set[str] | None = None) -> dict[str, Any]:
    steps = q.get("steps")
    if not isinstance(steps, list) or not steps:
        raise DesignError("a quest needs 'steps': a list of {id, objective, requires?, gives?, next | branches | end}.")
    by_id = {str(s.get("id")): s for s in steps if s.get("id")}
    start = str(q.get("start") or steps[0].get("id"))
    problems: list[str] = []
    if start not in by_id:
        raise DesignError(f"start step '{start}' does not exist.")

    def nxt(s: dict[str, Any]) -> list[str]:
        return [str(x) for x in ([s["next"]] if s.get("next") else []) + _text_list(s.get("branches"))]

    for sid, s in by_id.items():
        if not str(s.get("objective") or "").strip():
            problems.append(f"step '{sid}' has no objective.")
        for t in nxt(s):
            if t not in by_id:
                problems.append(f"step '{sid}' leads to missing step '{t}'.")
        if not nxt(s) and not s.get("end"):
            problems.append(f"step '{sid}' has no next step and is not marked end.")
    # Walk every path in order, carrying what the player holds, to find steps
    # that need something no earlier step gives (and the world does not).
    world = set(_text_list(q.get("world_items"))) | (known_items or set())
    unmet: list[dict[str, Any]] = []
    reachable, finishes = set(), False
    stack = [(start, frozenset(world), 0)]
    visits = 0
    while stack and visits < 5000:
        sid, have, depth = stack.pop()
        visits += 1
        s = by_id.get(sid)
        if s is None or depth > 200:
            continue
        reachable.add(sid)
        need = set(_text_list(s.get("requires")))
        missing = need - have
        if missing:
            unmet.append({"step": sid, "needs": sorted(missing)})
            continue
        got = have | set(_text_list(s.get("gives")))
        got -= set(_text_list(s.get("takes")))
        if s.get("end"):
            finishes = True
        for t in nxt(s):
            if (t, got) not in {(x[0], x[1]) for x in stack}:
                stack.append((t, frozenset(got), depth + 1))
    unreachable = sorted(set(by_id) - reachable)
    uniq_unmet = {(u["step"], tuple(u["needs"])) for u in unmet}
    out = {
        "id": q.get("id"),
        "steps": len(by_id),
        "problems": problems,
        "can_finish": finishes,
        "needs_not_given": [{"step": s, "needs": list(n)} for s, n in sorted(uniq_unmet)],
        "unreachable": unreachable,
        "rewards": _text_list(q.get("rewards")),
    }
    out["ok"] = not problems and finishes and not unreachable
    return out


def quest_rows(q: dict[str, Any]) -> list[dict[str, Any]]:
    qid = str(q["id"])
    rows = []
    for s in q["steps"]:
        rows.append({
            "Name": f"{qid}.{s['id']}",
            "Quest": qid,
            "Step": str(s["id"]),
            "Objective": str(s.get("objective") or ""),
            "Requires": ";".join(_text_list(s.get("requires"))),
            "Gives": ";".join(_text_list(s.get("gives"))),
            "Takes": ";".join(_text_list(s.get("takes"))),
            "Next": ";".join(f"{qid}.{x}" for x in ([s["next"]] if s.get("next") else []) + _text_list(s.get("branches"))),
            "Condition": str(s.get("condition") or ""),
            "End": bool(s.get("end")),
            "Rewards": ";".join(_text_list(q.get("rewards"))) if s.get("end") else "",
        })
    return rows


QUEST_COLUMNS = [("Quest", "name"), ("Step", "name"), ("Objective", "text"), ("Requires", "string"),
                 ("Gives", "string"), ("Takes", "string"), ("Next", "string"), ("Condition", "string"),
                 ("End", "bool"), ("Rewards", "string")]


# ------------------------------------------------------------------- balance


def sim_combat(attackers: list[dict[str, Any]], target: dict[str, Any], seconds: float = 300.0) -> dict[str, Any]:
    """Expected-value combat: who deals what, and how long the target lasts.

    Each attacker: {name, damage, interval (s between hits), hit_chance (0-1),
    count, crit_chance, crit_multiplier, start (s before its first hit)}.
    Target: {health, armor (flat per hit), resist (0-1), regen (per s),
    shield, heal_per_hit}. Expected values per hit; regen continuous."""
    hp = float(target.get("health") or 0)
    if hp <= 0:
        raise DesignError("target needs health above 0.")
    armor = float(target.get("armor") or 0)
    resist = min(max(float(target.get("resist") or 0), 0.0), 0.95)
    regen = float(target.get("regen") or 0)
    shield = float(target.get("shield") or 0)
    rows = []
    events: list[tuple[float, float, str]] = []
    total_dps = 0.0
    for a in attackers:
        dmg = float(a.get("damage") or 0)
        interval = float(a.get("interval") or 1.0)
        if interval <= 0:
            raise DesignError(f"attacker '{a.get('name', '?')}': interval must be above 0.")
        hit = min(max(float(a.get("hit_chance", 1.0)), 0.0), 1.0)
        crit = min(max(float(a.get("crit_chance") or 0), 0.0), 1.0)
        mult = float(a.get("crit_multiplier") or 2.0)
        count = int(a.get("count") or 1)
        per_hit = max(dmg - armor, 0.0) * (1 - resist)
        expected = per_hit * hit * (1 + crit * (mult - 1))
        dps = expected * count / interval
        total_dps += dps
        rows.append({"name": a.get("name", "attacker"), "count": count, "damage_after_armor": round(per_hit, 2),
                     "expected_per_hit": round(expected, 2), "dps": round(dps, 2)})
        t0 = float(a.get("start") or 0)
        n = int(seconds / interval) + 1
        for c in range(count):
            for k in range(n):
                t = t0 + k * interval
                if t <= seconds:
                    events.append((t, expected, str(a.get("name", "attacker"))))
    events.sort()
    pool = hp + shield
    last_t = 0.0
    curve = [(0.0, round(pool, 1))]
    death = None
    for t, d, _who in events:
        pool = min(pool + regen * (t - last_t), hp + shield) if regen else pool
        pool -= d
        last_t = t
        if len(curve) < 400:
            curve.append((round(t, 2), round(max(pool, 0), 1)))
        if pool <= 0:
            death = t
            break
    net = total_dps - regen
    out = {
        "attackers": rows,
        "total_dps": round(total_dps, 2),
        "net_dps_after_regen": round(net, 2),
        "time_to_kill_seconds": round(death, 2) if death is not None else None,
        "steady_state_estimate_seconds": round((hp + shield) / net, 2) if net > 0 else None,
        "survives": death is None,
        "health_curve": curve,
    }
    if death is None:
        out["note"] = f"The target outlasts {seconds:g} s: regen or armor beats the damage."
    return out


def sim_economy(sources: list[dict[str, Any]], sinks: list[dict[str, Any]], minutes: float = 120,
                start: float = 0, step_minutes: float = 5) -> dict[str, Any]:
    """Income and spending over play time.

    Source: {name, per_minute, growth (fraction per hour, e.g. 0.2), from_minute, until_minute}.
    Sink: {name, cost, every_minutes | at_minutes [..], growth (cost growth per purchase)}."""
    t, wallet = 0.0, float(start)
    series = []
    bought: dict[str, int] = {}
    broke_at = None
    income_total = spend_total = 0.0
    sink_state = {}
    for s in sinks:
        sink_state[s.get("name", "sink")] = {"cost": float(s.get("cost") or 0), "next": float(s.get("every_minutes") or 0) or None,
                                             "at": sorted(float(x) for x in s.get("at_minutes") or [])}
    while t <= minutes + 1e-9:
        inc = 0.0
        for src in sources:
            if float(src.get("from_minute") or 0) <= t <= float(src.get("until_minute") or 1e12):
                inc += float(src.get("per_minute") or 0) * (1 + float(src.get("growth") or 0)) ** (t / 60)
        wallet += inc * step_minutes
        income_total += inc * step_minutes
        for s in sinks:
            name = s.get("name", "sink")
            st = sink_state[name]
            due = False
            if st["at"] and st["at"][0] <= t:
                st["at"].pop(0)
                due = True
            elif st["next"] is not None and t >= st["next"]:
                st["next"] += float(s.get("every_minutes"))
                due = True
            if due:
                if wallet >= st["cost"]:
                    wallet -= st["cost"]
                    spend_total += st["cost"]
                    bought[name] = bought.get(name, 0) + 1
                    st["cost"] *= 1 + float(s.get("growth") or 0)
                elif broke_at is None:
                    broke_at = {"minute": round(t, 1), "wanted": name, "cost": round(st["cost"], 1), "had": round(wallet, 1)}
        series.append((round(t, 1), round(wallet, 1), round(inc, 2)))
        t += step_minutes
    out = {
        "earned": round(income_total, 1), "spent": round(spend_total, 1), "ending_balance": round(wallet, 1),
        "purchases": bought, "first_unaffordable": broke_at,
        "series_minute_balance_income": series,
    }
    if income_total > 0 and spend_total / income_total < 0.5:
        out["note"] = "Less than half of income is spent: currency will pile up (inflation)."
    return out


def sim_progression(levels: int, xp_formula: dict[str, Any], xp_per_minute: float,
                    xp_growth_per_level: float = 0.0) -> dict[str, Any]:
    """Time to reach each level. xp_formula: {type: linear|power|exponential, base, factor}."""
    kind = str(xp_formula.get("type") or "power")
    base = float(xp_formula.get("base") or 100)
    factor = float(xp_formula.get("factor") or 1.5)
    if levels < 2 or levels > 500 or xp_per_minute <= 0:
        raise DesignError("levels 2 to 500 and xp_per_minute above 0.")
    rows, total = [], 0.0
    for lv in range(2, levels + 1):
        n = lv - 1
        need = base * n if kind == "linear" else base * n ** factor if kind == "power" else base * factor ** (n - 1)
        rate = xp_per_minute * (1 + xp_growth_per_level) ** n
        mins = need / rate
        total += mins
        rows.append({"level": lv, "xp_to_reach": round(need), "minutes": round(mins, 1), "total_hours": round(total / 60, 2)})
    jumps = [r["level"] for i, r in enumerate(rows[1:], 1) if rows[i - 1]["minutes"] > 0 and r["minutes"] / rows[i - 1]["minutes"] > 1.6]
    return {"levels": rows, "hours_to_max": round(total / 60, 2), "steep_jumps_at": jumps}


def difficulty_curve(sections: list[dict[str, Any]], spike_ratio: float = 1.5) -> dict[str, Any]:
    """Sections: {name, player: target dict, enemies: attackers list, player_dps, enemy_health}.
    Difficulty = how fast the player dies against how fast they clear it."""
    rows = []
    for s in sections:
        c = sim_combat(s.get("enemies") or [], s.get("player") or {"health": 100})
        ttk_player = c["time_to_kill_seconds"] or 1e9
        clear = float(s.get("enemy_health") or 0) / max(float(s.get("player_dps") or 1), 1e-6)
        score = clear / ttk_player if ttk_player < 1e9 else 0.0
        rows.append({"section": s.get("name"), "player_dies_in_s": c["time_to_kill_seconds"],
                     "clear_time_s": round(clear, 1), "difficulty": round(score, 3)})
    spikes = []
    for i in range(1, len(rows)):
        a, b = rows[i - 1]["difficulty"], rows[i]["difficulty"]
        if a > 0 and b / a >= spike_ratio:
            spikes.append({"section": rows[i]["section"], "jump": round(b / a, 2)})
    for r in rows:
        if r["difficulty"] >= 1:
            r["warning"] = "the player dies before clearing it when standing and trading damage"
    return {"sections": rows, "spikes": spikes,
            "note": "difficulty = clear time / time the player survives; under 1 is winnable by trading hits, "
                    "and a smooth curve rises gently. Playtest numbers (hf_playtest) beat these estimates."}


# ------------------------------------------------------- rules for table rows


def check_rows(rows: list[dict[str, Any]], rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Rules: {field, min?, max?} or {budget: "<expr over fields>", max?, min?}.
    A budget expression uses + - * / ( ) numbers and field names, e.g.
    "damage / interval * 2 + health / 10"."""
    bad: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        for rule in rules:
            if "field" in rule:
                v = r.get(rule["field"])
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    bad.append({"row": i, "name": r.get("Name") or r.get("name"), "rule": rule, "why": "not a number"})
                    continue
                val = v
            elif "budget" in rule:
                val = _eval_budget(str(rule["budget"]), r)
            else:
                raise DesignError("a rule has 'field' or 'budget'.")
            if "min" in rule and val < float(rule["min"]) or "max" in rule and val > float(rule["max"]):
                bad.append({"row": i, "name": r.get("Name") or r.get("name"), "rule": rule, "value": round(val, 3)})
    vals = {}
    numeric = [k for k in (rows[0] if rows else {}) if all(_isnum(x.get(k)) for x in rows)]
    for k in numeric:
        xs = sorted(float(x[k]) for x in rows)
        med = xs[len(xs) // 2]
        outl = [x.get("Name") or x.get("name") or i for i, x in enumerate(rows)
                if med and (float(x[k]) > med * 3 or float(x[k]) < med / 3)]
        vals[k] = {"min": xs[0], "median": med, "max": xs[-1], "outliers": outl[:20]}
    return {"rows": len(rows), "broken_rules": bad, "columns": vals, "ok": not bad}


def _isnum(v: Any) -> bool:
    try:
        float(v)
        return not isinstance(v, bool)
    except (TypeError, ValueError):
        return False


_EXPR_OK = re.compile(r"^[\sA-Za-z0-9_.+\-*/()]+$")


def _eval_budget(expr: str, row: dict[str, Any]) -> float:
    if not _EXPR_OK.match(expr):
        raise DesignError(f"budget '{expr}' may use only numbers, field names and + - * / ( ).")

    def sub(m: re.Match[str]) -> str:
        name = m.group(0)
        if name not in row or not _isnum(row[name]):
            raise DesignError(f"budget '{expr}': field '{name}' is missing or not a number.")
        return repr(float(row[name]))

    py = re.sub(r"[A-Za-z_][A-Za-z0-9_]*", sub, expr)
    try:
        return float(eval(py, {"__builtins__": {}}, {}))  # noqa: S307 - only digits and operators remain
    except ZeroDivisionError:
        return math.inf
    except Exception as exc:  # noqa: BLE001
        raise DesignError(f"budget '{expr}' is not a valid expression: {exc}") from exc


# ---------------------------------------------------------- names, flavour


def check_names(names: list[str], bible: dict[str, Any] | None, similar: float = 0.85) -> dict[str, Any]:
    clean = [n.strip() for n in names if n and n.strip()]
    low = [n.lower() for n in clean]
    first: dict[str, str] = {}
    for n in clean:
        first.setdefault(n.lower(), n)
    dup = sorted({first[n.lower()] for n in clean if low.count(n.lower()) > 1})
    near = []
    for i in range(len(clean)):
        for j in range(i + 1, len(clean)):
            if low[i] != low[j] and SequenceMatcher(None, low[i], low[j]).ratio() >= similar:
                near.append([clean[i], clean[j]])
    clashes = []
    if bible:
        bn = bible_names(bible)
        for n in clean:
            if n.lower() in bn:
                clashes.append({"name": n, "already": bn[n.lower()]})
            else:
                for b, canon in bn.items():
                    if SequenceMatcher(None, n.lower(), b).ratio() >= similar:
                        clashes.append({"name": n, "too_close_to": canon})
                        break
    return {"count": len(clean), "duplicates": dup, "too_similar": near[:60], "clash_with_bible": clashes,
            "ok": not dup and not near and not clashes}


# ------------------------------------------------------------ engine tables


def rows_csv(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Name"] + [c for c, _ in columns])
    for r in rows:
        out = [r["Name"]]
        for c, t in columns:
            v = r.get(c, "")
            if t == "bool":
                v = "True" if v in (True, "true", "True", 1, "1") else "False"
            out.append(v)
        w.writerow(out)
    return buf.getvalue()


def _asset_path(folder: str, name: str) -> str:
    folder = "/" + str(folder or "/Game/Design").strip().strip("/")
    if not folder.startswith("/Game"):
        raise DesignError("'folder' must be under /Game.")
    return f"{folder}/{name}"


async def write_table(call: EngineCall, folder: str, struct_name: str, table_name: str,
                      columns: list[tuple[str, str]], rows: list[dict[str, Any]], replace: bool) -> dict[str, Any]:
    """Make (or reuse) a project-owned row struct and Data Table, and fill it.

    The struct and table are ordinary project assets: the game reads them
    with Get Data Table Row and needs nothing from HullForge at runtime."""
    struct = _asset_path(folder, struct_name)
    table = _asset_path(folder, table_name)
    steps = []
    ok, desc = await call("hf_describe_struct", {"struct": struct})
    created = False
    if not ok:
        created = True
        ok, r = await call("hf_create_struct", {"destination": struct})
        if not ok:
            raise DesignError(f"could not create {struct}: {r}")
        steps.append(f"created {struct}")
        ok, desc = await call("hf_describe_struct", {"struct": struct})
    have = _member_names(desc if ok else None)
    for col, typ in columns:
        if col.lower() not in {h.lower() for h in have}:
            ok, r = await call("hf_add_struct_member", {"struct": struct, "member": col, "type": typ})
            if not ok:
                raise DesignError(f"could not add {col} to {struct}: {r}")
            steps.append(f"added {col} ({typ})")
    # A struct made just now comes with a placeholder member: drop it. (Only
    # then: in a struct that already existed, a member is the project's.)
    wanted = {c.lower() for c, _ in columns}
    for m in have if created else []:
        if m.lower() not in wanted and re.match(r"^(MemberVar|NewVar)_?\d*$", m):
            ok, _r = await call("hf_remove_struct_member", {"struct": struct, "member": m, "accept_data_loss": True})
            if ok:
                steps.append(f"removed placeholder {m}")
    ok, _d = await call("hf_describe_data_table", {"table": table})
    if not ok:
        ok, r = await call("hf_create_data_table", {"destination": table, "row_struct": struct})
        if not ok:
            raise DesignError(f"could not create {table}: {r}")
        steps.append(f"created {table}")
    ok, r = await call("hf_import_data_table_csv", {"data_table": table, "text": rows_csv(rows, columns),
                                                    "format": "csv", "accept_replacement": True})
    if not ok:
        raise DesignError(f"could not fill {table}: {r}")
    steps.append(f"wrote {len(rows)} rows")
    return {"struct": struct, "data_table": table, "rows": len(rows), "steps": steps,
            "saved": False, "note": "In memory until saved (hf_save_all). The game reads rows with Get Data Table Row."}


def _member_names(desc: Any) -> list[str]:
    if not isinstance(desc, dict):
        return []
    for key in ("members", "properties", "fields"):
        ms = desc.get(key)
        if isinstance(ms, list):
            out = []
            for m in ms:
                if isinstance(m, dict):
                    n = m.get("member") or m.get("name") or m.get("friendly_name") or m.get("display_name")
                    if n:
                        out.append(str(n))
                elif isinstance(m, str):
                    out.append(m)
            return out
    return []


# ------------------------------------------------------------------- tools


@dataclass
class ToolDef:
    name: str
    kind: str
    description: str
    schema: dict[str, Any]


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_S = {"type": "string"}
_FOLDER = {"type": "string", "description": "Content folder for the struct and table, under /Game. Default /Game/Design."}
_ATTACKER = {"type": "object", "description": "{name, damage, interval (seconds between hits), hit_chance, count, crit_chance, crit_multiplier, start}"}
_TARGET = {"type": "object", "description": "{health, armor (flat per hit), resist (0-1), regen (per second), shield}"}

TOOLS: list[ToolDef] = [
    ToolDef("hf_design_doc", READ,
            "Read the game's design doc (pitch, pillars, core loop, feel, features, scope, out of scope, risks) "
            "and its findings. Start here for any game idea, and read it before designing a feature so the "
            "work follows the pillars. Kept in the project at HullForge/Design/design.json and design.md.",
            _obj({})),
    ToolDef("hf_write_design_doc", WRITE,
            "Create or update the game's design doc from a pitch: give only the sections that change; a "
            "section set to null is removed. Each save keeps the previous version in HullForge/Design/history. "
            "Sections: " + "; ".join(f"{k}: {v}" for k, v in DOC_SECTIONS.items()) + " Returns the doc's findings "
            "(missing sections, pillars that are not tests, too many 'must' features).",
            _obj({"sections": {"type": "object", "description": "Section name to value."}}, ["sections"])),
    ToolDef("hf_scope_check", READ,
            "Compare the design doc with what the project already has (the editor's project overview): the "
            "vertical slice (the 'must' features), what comes after, features whose 'needs' are not found in the "
            "project, a rough size, and the doc's findings.",
            _obj({})),
    ToolDef("hf_story_bible", READ,
            "Read the story bible: characters, factions, places, items, events and terms, with aliases, facts, "
            "voice and relations. Read it before writing story, dialogue, barks, quests or names.",
            _obj({"kind": {"type": "string", "enum": list(BIBLE_KINDS)}, "name": {"type": "string", "description": "One entry by name or alias."}})),
    ToolDef("hf_write_story_bible", WRITE,
            "Add or update story bible entries, or remove some. An entry: {kind (" + ", ".join(BIBLE_KINDS) + "), name, "
            "aliases[], summary, facts[] (short, checkable statements), voice (how they talk), when, relations "
            "{other name: relation}}. Fields not given keep their old values. Saved with history.",
            _obj({"entries": {"type": "array", "items": {"type": "object"}},
                  "remove": {"type": "array", "items": _S, "description": "Names to remove."}})),
    ToolDef("hf_check_text", READ,
            "Check text against the story bible: names that look like a misspelt bible name, capitalised names "
            "the bible does not know, and the facts of every entry the text mentions, so you can check for "
            "contradictions.",
            _obj({"texts": {"type": "array", "items": _S, "maxItems": 2000}}, ["texts"])),
    ToolDef("hf_check_dialogue", READ,
            "Check a branching conversation without saving it: dead ends, links to missing nodes, unreachable "
            "nodes, loops with no way out, empty or long lines, speakers and names not in the story bible. A "
            "conversation: {id, start, nodes: [{id, speaker, text, next | choices: [{text, next | end, condition}] "
            "| end: true, condition, sets: [flags]}]}. Give 'conversation', or 'id' of a saved one.",
            _obj({"conversation": {"type": "object"}, "id": _S})),
    ToolDef("hf_write_dialogue", WRITE,
            "Save a branching conversation (same shape as hf_check_dialogue) and write it into a Data Table the "
            "game reads: one row per line, named <conversation>.<node>, with speaker, line (localisable text), "
            "next row, up to four choices with their next row and condition, a condition, flags it sets, and End. "
            "The row struct and table are project assets (S_DialogueLine, DT_Dialogue by default), so the game "
            "needs nothing from HullForge. Refused while the check finds problems unless allow_problems is true. "
            "Voice lines can then be made with hf_generate_audio kind voice.",
            _obj({"conversation": {"type": "object"}, "folder": _FOLDER,
                  "table": {"type": "string", "description": "Data Table name. Default DT_Dialogue (all conversations share it)."},
                  "allow_problems": {"type": "boolean", "default": False}}, ["conversation"])),
    ToolDef("hf_write_barks", WRITE,
            "Save short situational lines (barks) and write them into a Data Table: {speaker, tag (e.g. "
            "Combat.Spotted, Idle, Hurt), text, weight, cooldown}. Duplicates and near duplicates are reported; "
            "names are checked against the story bible. The game picks a random row with the speaker and tag.",
            _obj({"set": {"type": "string", "description": "Name of this bark set, e.g. guards."},
                  "barks": {"type": "array", "items": {"type": "object"}, "maxItems": 2000},
                  "folder": _FOLDER, "table": {"type": "string", "description": "Default DT_Barks."}}, ["set", "barks"])),
    ToolDef("hf_check_quest", READ,
            "Check a quest without saving it: whether it can be finished, steps that need an item or flag no "
            "earlier step gives (and the world does not have), missing or unreachable steps, steps with no "
            "objective. A quest: {id, title, start, world_items[], rewards[], steps: [{id, objective, requires[], "
            "gives[], takes[], next | branches[], condition, end}]}. Give 'quest', or 'id' of a saved one.",
            _obj({"quest": {"type": "object"}, "id": _S})),
    ToolDef("hf_write_quest", WRITE,
            "Save a quest (same shape as hf_check_quest) and write its steps into a Data Table: one row per step, "
            "named <quest>.<step>, with objective, requires, gives, takes, next steps, condition, End and rewards. "
            "Refused while the check finds problems unless allow_problems is true.",
            _obj({"quest": {"type": "object"}, "folder": _FOLDER, "table": {"type": "string", "description": "Default DT_Quests."},
                  "allow_problems": {"type": "boolean", "default": False}}, ["quest"])),
    ToolDef("hf_write_tuning", WRITE,
            "Turn a mechanic into a spec and a tuning table the code reads, so a designer changes numbers without "
            "touching code. Give the mechanic {name, summary, rules[] (how it works, plainly), values: [{name, "
            "value, unit, min, max, notes}]}. Saved to HullForge/Design/mechanics and written to a Data Table "
            "(DT_Tuning by default) with one row per value named <mechanic>.<value>: Value, Min, Max, Unit, Notes. "
            "Read it in Blueprint with Get Data Table Row, or in C++ with FindRow.",
            _obj({"mechanic": {"type": "object"}, "folder": _FOLDER, "table": {"type": "string", "description": "Default DT_Tuning."}}, ["mechanic"])),
    ToolDef("hf_balance_sim", READ,
            "Balance arithmetic, done exactly. mode combat: how long a target lasts against attackers (expected "
            "damage per hit after armor, resist, hit and crit chance; regen; a health curve). mode economy: "
            "wallet over play time from sources and sinks (first purchase the player cannot afford, inflation). "
            "mode progression: time to each level from an XP curve and XP per minute (steep jumps). mode "
            "difficulty: a difficulty score per section with spikes flagged. Check the answers against a "
            "playtest (hf_playtest) for the real thing.",
            _obj({"mode": {"type": "string", "enum": ["combat", "economy", "progression", "difficulty"]},
                  "attackers": {"type": "array", "items": _ATTACKER}, "target": _TARGET,
                  "seconds": {"type": "number", "default": 300},
                  "sources": {"type": "array", "items": {"type": "object", "description": "{name, per_minute, growth, from_minute, until_minute}"}},
                  "sinks": {"type": "array", "items": {"type": "object", "description": "{name, cost, every_minutes | at_minutes[], growth}"}},
                  "minutes": {"type": "number", "default": 120}, "start": {"type": "number", "default": 0},
                  "levels": {"type": "integer"}, "xp_formula": {"type": "object", "description": "{type: linear|power|exponential, base, factor}"},
                  "xp_per_minute": {"type": "number"}, "xp_growth_per_level": {"type": "number", "default": 0},
                  "sections": {"type": "array", "items": {"type": "object", "description": "{name, player (target), enemies (attackers), player_dps, enemy_health}"}}},
                 ["mode"])),
    ToolDef("hf_check_rows", READ,
            "Check generated table rows (items, enemies, loot) against balance rules before writing them: "
            "per-field ranges {field, min, max} and power budgets {budget: 'damage / interval + health / 10', "
            "max}. Also lists each numeric column's range and outliers (over 3x or under a third of the median).",
            _obj({"rows": {"type": "array", "items": {"type": "object"}, "maxItems": 5000},
                  "rules": {"type": "array", "items": {"type": "object"}}}, ["rows", "rules"])),
    ToolDef("hf_write_game_table", WRITE,
            "Write item, enemy, loot or any other rows into a project Data Table with a project-owned row "
            "struct, after checking them against balance rules (refused if a rule breaks, unless allow_problems). "
            "Columns: {name: type} with types string, name, text, int, float, bool. Each row needs 'Name'. Saved "
            "too in HullForge/Design/tables.",
            _obj({"table": {"type": "string", "description": "Data Table name, e.g. DT_Items."},
                  "struct": {"type": "string", "description": "Row struct name, e.g. S_Item."},
                  "columns": {"type": "object"}, "rows": {"type": "array", "items": {"type": "object"}, "maxItems": 5000},
                  "rules": {"type": "array", "items": {"type": "object"}}, "folder": _FOLDER,
                  "allow_problems": {"type": "boolean", "default": False}}, ["table", "struct", "columns", "rows"])),
    ToolDef("hf_check_names", READ,
            "Check a batch of names (characters, places, items) for duplicates, names too alike to tell apart, "
            "and clashes with the story bible.",
            _obj({"names": {"type": "array", "items": _S, "maxItems": 2000},
                  "similarity": {"type": "number", "default": 0.85}}, ["names"])),
]

TOOL_NAMES = frozenset(t.name for t in TOOLS)
WRITE_TOOLS = frozenset(t.name for t in TOOLS if t.kind == WRITE)


# ----------------------------------------------------------------- handlers


async def _call_tool_core(name: str, args: dict[str, Any], store: Store, engine: EngineCall | None) -> dict[str, Any]:
    """The tools that only need files and the editor's table tools."""
    bible = store.load("bible.json", default={"entries": {}})
    if name == "hf_design_doc":
        doc = store.load("design.json")
        if doc is None:
            return {"doc": None, "next": "No design doc yet: write one with hf_write_design_doc from the pitch.",
                    "sections": DOC_SECTIONS}
        return {"doc": doc, "findings": doc_findings(doc), "file": str(store.path("design.md"))}
    if name == "hf_write_design_doc":
        sec = args.get("sections")
        if not isinstance(sec, dict) or not sec:
            raise DesignError("'sections' is an object of the sections to set.")
        doc = merge_doc(store.load("design.json", default={}), sec)
        p = store.save(doc, "design.json", markdown=doc_markdown(doc), history=True)
        return {"saved": str(p.with_suffix(".md")), "findings": doc_findings(doc), "sections": sorted(k for k in doc if k in DOC_SECTIONS)}
    if name == "hf_scope_check":
        doc = store.load("design.json")
        if doc is None:
            raise DesignError("no design doc yet: write one with hf_write_design_doc first.")
        overview = None
        if engine is not None:
            ok, r = await engine("hf_get_project_overview", {"top": 20})
            overview = r if ok else None
        out = scope_report(doc, overview)
        if overview is None:
            out["note"] = "No editor connected: the project was not compared."
        return out
    if name == "hf_story_bible":
        entries = list((bible.get("entries") or {}).values())
        kind, nm = args.get("kind"), str(args.get("name") or "").lower()
        if kind:
            entries = [e for e in entries if e["kind"] == kind]
        if nm:
            canon = bible_names(bible).get(nm)
            entries = [e for e in entries if canon and e["name"] == canon]
        return {"entries": entries, "count": len(entries), "file": str(store.path("bible.md"))}
    if name == "hf_write_story_bible":
        changed = bible_upsert(bible, args.get("entries") or [])
        removed = []
        for r in _text_list(args.get("remove")):
            if (bible.get("entries") or {}).pop(r.lower(), None):
                removed.append(r)
        if not changed and not removed:
            raise DesignError("give 'entries' to add or update, or 'remove'.")
        store.save(bible, "bible.json", markdown=bible_markdown(bible), history=True)
        return {"updated": changed, "removed": removed, "entries": len(bible.get("entries") or {})}
    if name == "hf_check_text":
        return check_text(bible, _text_list(args.get("texts")))
    if name in ("hf_check_dialogue", "hf_write_dialogue"):
        conv = args.get("conversation")
        if conv is None and args.get("id"):
            conv = store.load("dialogue", f"{safe_id(args['id'], 'id')}.json")
            if conv is None:
                raise DesignError(f"no saved conversation '{args['id']}'.")
        if not isinstance(conv, dict):
            raise DesignError("give 'conversation' (or 'id' of a saved one).")
        cid = safe_id(conv.get("id"), "conversation.id")
        report = check_dialogue(conv, bible)
        if name == "hf_check_dialogue":
            return report
        if not report["ok"] and not args.get("allow_problems"):
            raise DesignError("the conversation has problems; fix them or pass allow_problems: " + json.dumps(
                {k: report[k] for k in ("problems", "dead_ends", "dangling_links", "unreachable", "loops_without_exit", "empty_lines") if report[k]}))
        store.save(conv, "dialogue", f"{cid}.json")
        out = {"check": report, "saved": str(store.path("dialogue", f"{cid}.json"))}
        if engine is not None:
            out["engine"] = await _write_merged(engine, store, args, "dialogue", "S_DialogueLine",
                                                str(args.get("table") or "DT_Dialogue"), DIALOGUE_COLUMNS, dialogue_rows)
        else:
            out["engine"] = "No editor connected: saved to the Design folder only."
        return out
    if name == "hf_write_barks":
        set_id = safe_id(args.get("set"), "set")
        barks = args.get("barks") or []
        if not isinstance(barks, list) or not barks:
            raise DesignError("'barks' is a list of {speaker, tag, text}.")
        report = check_barks(barks, bible)
        data = {"id": set_id, "barks": barks}
        store.save(data, "barks", f"{set_id}.json")
        out = {"check": report}
        if engine is not None:
            def rows_of(d: dict[str, Any]) -> list[dict[str, Any]]:
                return [{"Name": f"{d['id']}.{i + 1}", "Speaker": b.get("speaker", ""), "Tag": b.get("tag", ""),
                         "Line": b.get("text", ""), "Weight": float(b.get("weight", 1)), "Cooldown": float(b.get("cooldown", 0))}
                        for i, b in enumerate(d["barks"])]
            out["engine"] = await _write_merged(engine, store, args, "barks", "S_Bark", str(args.get("table") or "DT_Barks"),
                                                BARK_COLUMNS, rows_of)
        return out
    if name in ("hf_check_quest", "hf_write_quest"):
        q = args.get("quest")
        if q is None and args.get("id"):
            q = store.load("quests", f"{safe_id(args['id'], 'id')}.json")
            if q is None:
                raise DesignError(f"no saved quest '{args['id']}'.")
        if not isinstance(q, dict):
            raise DesignError("give 'quest' (or 'id' of a saved one).")
        qid = safe_id(q.get("id"), "quest.id")
        report = check_quest(q)
        if name == "hf_check_quest":
            return report
        if not report["ok"] and not args.get("allow_problems"):
            raise DesignError("the quest has problems; fix them or pass allow_problems: " + json.dumps(report))
        store.save(q, "quests", f"{qid}.json")
        out = {"check": report, "saved": str(store.path("quests", f"{qid}.json"))}
        if engine is not None:
            out["engine"] = await _write_merged(engine, store, args, "quests", "S_QuestStep",
                                                str(args.get("table") or "DT_Quests"), QUEST_COLUMNS, quest_rows)
        return out
    if name == "hf_write_tuning":
        m = args.get("mechanic")
        if not isinstance(m, dict):
            raise DesignError("'mechanic' is {name, summary, rules[], values[]}.")
        mid = safe_id(m.get("name"), "mechanic.name")
        values = m.get("values") or []
        if not values:
            raise DesignError("a mechanic needs 'values' to tune.")
        for v in values:
            safe_id(v.get("name"), "value name")
            if not _isnum(v.get("value")):
                raise DesignError(f"value '{v.get('name')}' needs a numeric 'value'.")
            lo, hi = v.get("min"), v.get("max")
            if _isnum(lo) and _isnum(hi) and not float(lo) <= float(v["value"]) <= float(hi):
                raise DesignError(f"value '{v['name']}' = {v['value']} is outside its own min/max.")
        md = [f"# {m['name']}", "", str(m.get("summary") or ""), "", "## Rules", ""] + [f"- {r}" for r in _text_list(m.get("rules"))] + \
             ["", "## Values", "", "| Value | Default | Range | Unit | Notes |", "| --- | --- | --- | --- | --- |"] + \
             [f"| {v['name']} | {v['value']} | {v.get('min', '')} to {v.get('max', '')} | {v.get('unit', '')} | {v.get('notes', '')} |" for v in values]
        store.save(m, "mechanics", f"{mid}.json", markdown="\n".join(md) + "\n")
        out = {"saved": str(store.path("mechanics", f"{mid}.md"))}
        if engine is not None:
            cols = [("Value", "float"), ("Min", "float"), ("Max", "float"), ("Unit", "name"), ("Notes", "string")]

            def rows_of(d: dict[str, Any]) -> list[dict[str, Any]]:
                return [{"Name": f"{d['name']}.{v['name']}", "Value": float(v["value"]),
                         "Min": float(v["min"]) if _isnum(v.get("min")) else 0.0,
                         "Max": float(v["max"]) if _isnum(v.get("max")) else 0.0,
                         "Unit": str(v.get("unit") or ""), "Notes": str(v.get("notes") or "")} for v in d["values"]]
            out["engine"] = await _write_merged(engine, store, args, "mechanics", "S_TuningValue",
                                                str(args.get("table") or "DT_Tuning"), cols, rows_of)
            out["read_in_code"] = (f"Blueprint: Get Data Table Row (table {out['engine'].get('data_table')}, row "
                                   f"'{mid}.<value>') then Value. C++: Table->FindRow<...>(FName(\"{mid}.<value>\")) or "
                                   f"read the struct's Value property.")
        return out
    if name == "hf_balance_sim":
        mode = args.get("mode")
        if mode == "combat":
            return sim_combat(args.get("attackers") or [], args.get("target") or {}, float(args.get("seconds") or 300))
        if mode == "economy":
            return sim_economy(args.get("sources") or [], args.get("sinks") or [], float(args.get("minutes") or 120),
                               float(args.get("start") or 0))
        if mode == "progression":
            return sim_progression(int(args.get("levels") or 0), args.get("xp_formula") or {},
                                   float(args.get("xp_per_minute") or 0), float(args.get("xp_growth_per_level") or 0))
        if mode == "difficulty":
            return difficulty_curve(args.get("sections") or [])
        raise DesignError("mode is combat, economy, progression or difficulty.")
    if name == "hf_check_rows":
        return check_rows(args.get("rows") or [], args.get("rules") or [])
    if name == "hf_write_game_table":
        table = safe_id(args.get("table"), "table")
        struct = safe_id(args.get("struct"), "struct")
        cols_in = args.get("columns")
        if not isinstance(cols_in, dict) or not cols_in:
            raise DesignError("'columns' is {name: type}.")
        allowed = {"string", "name", "text", "int", "float", "bool"}
        cols = []
        for c, t in cols_in.items():
            safe_id(c, "column")
            if str(t).lower() not in allowed:
                raise DesignError(f"column '{c}': type is one of {sorted(allowed)}.")
            if c == "Name":
                raise DesignError("'Name' is the row name, not a column.")
            cols.append((c, str(t).lower()))
        rows = args.get("rows") or []
        for i, r in enumerate(rows):
            if not str(r.get("Name") or "").strip():
                raise DesignError(f"row {i} has no 'Name'.")
        names = [str(r["Name"]) for r in rows]
        if len(set(names)) != len(names):
            raise DesignError("row names repeat: " + ", ".join(sorted({n for n in names if names.count(n) > 1})[:10]))
        report = check_rows(rows, args.get("rules") or []) if args.get("rules") else None
        if report and not report["ok"] and not args.get("allow_problems"):
            raise DesignError("rows break the rules; fix them or pass allow_problems: " + json.dumps(report["broken_rules"][:20]))
        store.save({"table": table, "struct": struct, "columns": cols_in, "rows": rows, "rules": args.get("rules") or []},
                   "tables", f"{table}.json")
        out: dict[str, Any] = {"check": report}
        if engine is not None:
            out["engine"] = await write_table(engine, str(args.get("folder") or "/Game/Design"), struct, table, cols, rows, True)
        return out
    if name == "hf_check_names":
        return check_names(_text_list(args.get("names")), bible, float(args.get("similarity") or 0.85))
    raise DesignError(f"'{name}' is not a design tool.")


async def _write_merged(engine: EngineCall, store: Store, args: dict[str, Any], kind: str, struct: str, table: str,
                        columns: list[tuple[str, str]], rows_of: Callable[[dict[str, Any]], list[dict[str, Any]]]) -> dict[str, Any]:
    """Every saved item of this kind shares one table: rebuild all its rows."""
    rows: list[dict[str, Any]] = []
    folder = store.path(kind)
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            rows += rows_of(json.loads(f.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError) as exc:
            raise DesignError(f"{f.name} in the Design folder could not be read ({exc}).") from exc
    return await write_table(engine, str(args.get("folder") or "/Game/Design"), struct, table, columns, rows, True)


# ------------------------------------------------------------------ blockout

BEAT_KINDS = {
    # kind: (length m, width m, floor height m)
    "corridor": (20, 4, 0), "room": (12, 12, 0), "arena": (30, 30, 0), "ledge": (10, 8, 3),
    "cover": (16, 14, 0), "climb": (10, 6, 0), "open": (40, 40, 0), "chokepoint": (8, 3, 0),
}
_CUBE = "/Engine/BasicShapes/Cube.Cube"


def blockout_plan(beats: list[dict[str, Any]], gap_m: float = 2.0, wall_m: float = 4.0) -> list[dict[str, Any]]:
    """Boxes (location and size in cm) for each beat, laid end to end along X."""
    if not beats or len(beats) > 30:
        raise DesignError("give 1 to 30 beats.")
    out, x = [], 0.0
    for i, b in enumerate(beats):
        kind = str(b.get("kind") or "room").lower()
        if kind not in BEAT_KINDS:
            raise DesignError(f"beat {i + 1}: kind is one of {sorted(BEAT_KINDS)}.")
        length, width, rise = BEAT_KINDS[kind]
        size = b.get("size_m")
        if isinstance(size, list) and len(size) >= 2:
            length, width = float(size[0]), float(size[1])
        if not (1 <= length <= 300 and 1 <= width <= 300):
            raise DesignError(f"beat {i + 1}: size_m must be 1 to 300 metres each way.")
        rise = float(b.get("height_m", rise))
        name = re.sub(r"[^A-Za-z0-9_]", "_", str(b.get("name") or kind))[:40]
        folder = f"Blockout/{i + 1:02d}_{name}"
        L, W, H = length * 100, width * 100, rise * 100
        cx = x + L / 2
        boxes = [{"part": "Floor", "loc": (cx, 0, H - 10), "size": (L, W, 20)}]
        if H > 0:
            boxes.append({"part": "Riser", "loc": (cx, 0, (H - 20) / 2), "size": (L, W, max(H - 20, 1))})
        if kind != "open":
            for side in (-1, 1):
                boxes.append({"part": "Wall", "loc": (cx, side * (W / 2 + 10), H + wall_m * 50), "size": (L, 20, wall_m * 100)})
        if kind == "cover":
            for k in range(4):
                boxes.append({"part": "Cover", "loc": (x + L * (k + 1) / 5, (-1) ** k * W / 5, H + 60), "size": (100, 250, 120)})
        if kind == "climb":
            for k in range(5):
                boxes.append({"part": "Step", "loc": (x + 150 + k * 150, 0, H + 20 * (k + 1)), "size": (150, W * 0.6, 40 * (k + 1))})
        if kind == "chokepoint":
            boxes.append({"part": "Door", "loc": (x + L, 0, H + wall_m * 50), "size": (20, W / 3, wall_m * 100)})
        n = int(b.get("enemies") or 0)
        for k in range(min(n, 30)):
            boxes.append({"part": "Enemy", "loc": (x + L * 0.6 + (k % 3) * 150, (k // 3 - 1) * 200, H + 100), "size": (60, 60, 180)})
        if i == 0:
            boxes.append({"part": "PlayerStart", "loc": (x + 200, 0, H + 100), "size": (60, 60, 180)})
        out.append({"beat": i + 1, "name": name, "kind": kind, "folder": folder, "notes": str(b.get("notes") or ""),
                    "boxes": boxes, "start_x_cm": x, "length_cm": L})
        x += L
        if i < len(beats) - 1:
            # A short bridge to the next beat, at the next beat's height.
            nxt_rise = float(beats[i + 1].get("height_m", BEAT_KINDS.get(str(beats[i + 1].get("kind") or "room").lower(), (0, 0, 0))[2])) * 100
            g = gap_m * 100
            out[-1]["boxes"].append({"part": "Link", "loc": (x + g / 2, 0, max(H, nxt_rise) - 10), "size": (g, 300, 20)})
            x += g
    return out


def _actor_of(result: Any) -> str:
    if isinstance(result, dict):
        for k in ("label", "actor", "name", "actor_name", "actor_label", "path"):
            v = result.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                for kk in ("label", "name", "path"):
                    if isinstance(v.get(kk), str):
                        return v[kk]
    return ""


async def build_blockout(call: EngineCall, plan: list[dict[str, Any]], origin: tuple[float, float, float]) -> dict[str, Any]:
    spawned, failed = 0, []
    ox, oy, oz = origin
    for beat in plan:
        for box in beat["boxes"]:
            lx, ly, lz = box["loc"]
            sx, sy, sz = (v / 100 for v in box["size"])
            ok, r = await call("hf_spawn_actor_from_asset", {
                # The engine cube is 1 m across with its pivot at its centre.
                "asset_path": _CUBE, "location": {"x": lx + ox, "y": ly + oy, "z": lz + oz},
                "scale": {"x": sx, "y": sy, "z": sz}})
            if not ok:
                failed.append({"beat": beat["beat"], "part": box["part"], "error": str(r)[:200]})
                continue
            spawned += 1
            actor = _actor_of(r)
            if actor:
                await call("hf_set_actor_folder", {"actor": actor, "folder": f"{beat['folder']}/{box['part']}s"
                                                   if box["part"] in ("Wall", "Cover", "Step", "Enemy") else beat["folder"]})
    return {"spawned": spawned, "failed": failed[:20]}


# ---------------------------------------------------------------- playtests


def jitter_steps(steps: list[dict[str, Any]], amount: float, rng: Any) -> list[dict[str, Any]]:
    """A varied copy of a playtest script: waits stretched, inputs and move
    targets nudged, so many runs cover more than one exact path."""
    out = []
    for s in steps:
        s = json.loads(json.dumps(s))
        for k in ("seconds", "duration", "hold_seconds"):
            if isinstance(s.get(k), (int, float)):
                s[k] = round(max(0.0, s[k] * (1 + rng.uniform(-amount, amount))), 3)
        for k in ("value", "axis"):
            if isinstance(s.get(k), (int, float)) and s.get("type") == "input":
                s[k] = round(max(-1.0, min(1.0, s[k] * (1 + rng.uniform(-amount, amount)))), 3)
        loc = s.get("location") if s.get("type") in ("move_to", "teleport") else None
        if isinstance(loc, str) and loc.count(",") == 2:
            try:
                x, y, z = (float(v) for v in loc.split(","))
                r = amount * 500
                s["location"] = f"{x + rng.uniform(-r, r):.0f},{y + rng.uniform(-r, r):.0f},{z:.0f}"
            except ValueError:
                pass
        out.append(s)
    return out


def summarize_runs(reports: list[dict[str, Any]], sections: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Win rate, where runs failed and why, and time per section.

    Reads hf_pie_get_playtest reports: status passed|failed|aborted|error,
    elapsed_seconds, and steps [{step (1-based), type, name?, status,
    seconds (how long the step took), message?}]. A section is
    {name, from_step, to_step}, step numbers inclusive."""
    n = len(reports)
    passed = 0
    fail_at: dict[str, int] = {}
    reasons: dict[str, int] = {}
    durations: list[float] = []
    sec_times: dict[str, list[float]] = {str(s.get("name", f"s{i}")): [] for i, s in enumerate(sections or [])}
    for rep in reports:
        status = rep.get("status")
        ok = status == "passed" if isinstance(status, str) else bool(rep.get("ok"))
        passed += ok
        steps = [st for st in (rep.get("steps") or []) if isinstance(st, dict)]
        total = rep.get("elapsed_seconds")
        if isinstance(total, (int, float)):
            durations.append(float(total))
        for st in steps:
            if st.get("status") in ("failed", "error") or st.get("ok") is False:
                where = str(st.get("name") or f"step {st.get('step', '?')} ({st.get('type', '')})")
                fail_at[where] = fail_at.get(where, 0) + 1
                why = str(st.get("message") or st.get("detail") or st.get("error") or st.get("reason") or "failed")[:120]
                reasons[why] = reasons.get(why, 0) + 1
                break
        by_num = {int(st["step"]): st for st in steps if isinstance(st.get("step"), int)}
        for i, sec in enumerate(sections or []):
            a, b = int(sec.get("from_step", 1)), int(sec.get("to_step", 1))
            span = [by_num.get(k) for k in range(a, b + 1)]
            if span and all(x is not None and x.get("status") == "passed" and isinstance(x.get("seconds"), (int, float)) for x in span):
                sec_times[str(sec.get("name", f"s{i}"))].append(sum(float(x["seconds"]) for x in span))  # type: ignore[index]

    def stats(xs: list[float]) -> dict[str, Any]:
        if not xs:
            return {"completed_runs": 0}
        xs = sorted(xs)
        return {"completed_runs": len(xs), "min": round(xs[0], 2), "median": round(xs[len(xs) // 2], 2), "max": round(xs[-1], 2)}
    return {
        "runs": n, "passed": passed, "pass_rate": round(passed / n, 3) if n else None,
        "failed_at_step": dict(sorted(fail_at.items(), key=lambda kv: -kv[1])),
        "failure_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:15]),
        "run_seconds": stats(durations),
        "section_seconds": {k: stats(v) for k, v in sec_times.items()},
    }


TOOLS += [
    ToolDef("hf_blockout_from_beats", WRITE,
            "Turn a written beat sheet into a playable graybox in the open level: each beat {name, kind ("
            + ", ".join(sorted(BEAT_KINDS)) + "), size_m [length, width], height_m, enemies (count of markers), notes} "
            "becomes floor, walls, cover, steps or a door from engine cubes, laid end to end along X from 'origin', "
            "joined by short links, each in an Outliner folder Blockout/NN_<beat>. Enemy and player-start markers "
            "are tall thin boxes. The beat sheet and notes are saved in HullForge/Design/blockouts. Undo with the "
            "editor's undo or delete the Blockout folder.",
            _obj({"id": _S, "beats": {"type": "array", "items": {"type": "object"}, "maxItems": 30},
                  "origin": {"type": "string", "description": "X,Y,Z in cm. Default 0,0,0."},
                  "plan_only": {"type": "boolean", "default": False, "description": "Return the boxes without spawning."}},
                 ["id", "beats"])),
    ToolDef("hf_playtest_batch", WRITE,
            "Run the same playtest many times (a playtest bot) and report the pass rate, where and why runs failed "
            "(deaths by cause, when the script expects survival), and time per section. 'steps' is an "
            "hf_pie_run_playtest script; with 'vary' (0 to 0.5) each run stretches waits and nudges inputs and "
            "move targets so runs differ. Sections: [{name, from_step, to_step}]. Starts and stops PIE each run. "
            "Up to 50 runs; the summary is saved in HullForge/Design/playtests.",
            _obj({"id": _S, "steps": {"type": "array", "items": {"type": "object"}, "maxItems": 100},
                  "runs": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                  "vary": {"type": "number", "minimum": 0, "maximum": 0.5, "default": 0.15},
                  "sections": {"type": "array", "items": {"type": "object"}},
                  "seed": {"type": "integer"}}, ["id", "steps"])),
]
TOOL_NAMES = frozenset(t.name for t in TOOLS)
WRITE_TOOLS = frozenset(t.name for t in TOOLS if t.kind == WRITE)

async def call_tool(name: str, args: dict[str, Any], store: Store, engine: EngineCall | None,
                    run_playtest: Callable[[dict[str, Any]], Awaitable[tuple[bool, Any]]] | None = None) -> dict[str, Any]:
    """Run one design tool. Raises DesignError for a refusal."""
    if name == "hf_blockout_from_beats":
        bid = safe_id(args.get("id"), "id")
        plan = blockout_plan(args.get("beats") or [])
        try:
            origin = tuple(float(v) for v in str(args.get("origin") or "0,0,0").split(","))
            assert len(origin) == 3
        except (ValueError, AssertionError) as exc:
            raise DesignError("'origin' is X,Y,Z in cm.") from exc
        md = [f"# Blockout {bid}", ""] + [f"{p['beat']}. **{p['name']}** ({p['kind']}): {p['notes']}" for p in plan]
        store.save({"id": bid, "beats": args.get("beats"), "origin": args.get("origin") or "0,0,0"}, "blockouts", f"{bid}.json",
                   markdown="\n".join(md) + "\n")
        out: dict[str, Any] = {"beats": [{k: p[k] for k in ("beat", "name", "kind", "folder", "notes")} | {"boxes": len(p["boxes"])} for p in plan],
                               "length_m": round((plan[-1]["start_x_cm"] + plan[-1]["length_cm"]) / 100, 1)}
        if args.get("plan_only"):
            out["plan"] = plan
            return out
        if engine is None:
            raise DesignError("the editor is not connected, so nothing was spawned (plan_only works without it).")
        out["spawn"] = await build_blockout(engine, plan, origin)  # type: ignore[arg-type]
        out["next"] = "Play it (hf_pie_run_playtest or PIE) and tune sizes in the beat sheet, then run this again elsewhere."
        return out
    if name == "hf_playtest_batch":
        import random
        pid = safe_id(args.get("id"), "id")
        steps = args.get("steps") or []
        if not steps:
            raise DesignError("'steps' is an hf_pie_run_playtest script.")
        if run_playtest is None:
            raise DesignError("playtests need the editor connected.")
        runs = max(1, min(int(args.get("runs") or 10), 50))
        vary = max(0.0, min(float(args.get("vary") if args.get("vary") is not None else 0.15), 0.5))
        rng = random.Random(args.get("seed"))
        reports, errors = [], []
        for i in range(runs):
            ok, rep = await run_playtest({"steps": jitter_steps(steps, vary, rng) if vary else steps,
                                          "start_pie": True, "stop_pie_at_end": True, "stop_on_fail": True,
                                          "capture_on_fail": False, "wait_seconds": 120})
            if isinstance(rep, dict):
                reports.append(rep)
            else:
                errors.append(str(rep)[:200])
                if len(errors) >= 3 and not reports:
                    raise DesignError("the first runs did not start: " + " | ".join(errors))
        summary = summarize_runs(reports, args.get("sections"))
        summary["errors"] = errors[:10]
        store.save({"id": pid, "runs": runs, "vary": vary, "summary": summary}, "playtests", f"{pid}.json")
        return summary
    return await _call_tool_core(name, args, store, engine)
