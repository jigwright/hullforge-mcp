# Copyright (c) 2026 Jigwright. All rights reserved.
"""What happened to an editor that went away.

When the editor's process is gone, the most useful thing the connector can say
is WHY. Unreal's crash reporter writes a folder per crash under
<Project>/Saved/Crashes/, holding CrashContext.runtime-xml (the error message
and the call stack) and a copy of the log. This reads the newest one written
since the connection began and boils it down to what an agent needs to tell
the person and decide what to do next: the error, the top of the call stack,
and the last error lines of the log.

A process that is gone with no new crash folder was closed or killed rather
than crashed, and that is reported as such - never guessed.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

_STACK_LINES = 12
_LOG_LINES = 10
# Unreal's own markers for an error line, not the word "error" anywhere: a
# test called "is an ERROR when..." is not an error.
_LOG_PATTERN = re.compile(
    r"(: Error: |: Fatal: |Fatal error|Assertion failed|Unhandled Exception|=== Critical error ===)")
# Lines every editor log has, crash or not: optional profiler DLLs that are
# not installed, and the crash reporter's own bookkeeping.
_LOG_NOISE = re.compile(r"(Failed to load '.*\.dll'|SubmitErrorReport|=== Handled ensure)", re.IGNORECASE)
# An ensure is reported and the editor carries on, so a folder of this type
# never explains why the editor went away.
_NON_FATAL = {"ensure"}


def _tag(text: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    return html.unescape(m.group(1)).strip() if m else ""


_TAIL_BYTES = 256 * 1024
_TAIL_LINES = 8


def _log_facts(folder: Path) -> dict[str, list[str]]:
    """The last lines the editor logged, and any error lines among them.

    Only the end of the log is read: it can be tens of megabytes, and what
    matters is what the editor was doing when it stopped. The copy in a crash
    folder is often flushed a moment before the crash itself, so the error
    lines can be empty while the last lines still say what was running.
    """
    logs = sorted(folder.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {}
    try:
        with logs[0].open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]  # the first line of the chunk is usually cut
    out: dict[str, list[str]] = {}
    errors = [ln for ln in lines if _LOG_PATTERN.search(ln) and not _LOG_NOISE.search(ln)]
    if errors:
        out["log_errors"] = errors[-_LOG_LINES:]
    if lines:
        out["log_tail"] = lines[-_TAIL_LINES:]
    return out


def find_latest_crash(project_dir: str, since: float) -> dict[str, Any] | None:
    """The newest crash folder written at or after `since` (epoch seconds), summarized.

    None when there is no such folder: the editor was closed or killed, or the
    crash reporter did not run.
    """
    if not project_dir:
        return None
    root = Path(project_dir) / "Saved" / "Crashes"
    if not root.is_dir():
        return None

    folders = []
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime >= since - 5:
                folders.append(d)
        except OSError:
            continue

    # Newest first; skip ensures, which the editor survives.
    ensures = 0
    chosen: tuple[Path, str] | None = None
    for d in sorted(folders, key=lambda d: d.stat().st_mtime, reverse=True):
        ctx = d / "CrashContext.runtime-xml"
        try:
            text = ctx.read_text(encoding="utf-8", errors="replace") if ctx.is_file() else ""
        except OSError:
            text = ""
        if _tag(text, "CrashType").lower() in _NON_FATAL:
            ensures += 1
            continue
        chosen = (d, text)
        break
    if chosen is None:
        return None
    folder, text = chosen

    summary: dict[str, Any] = {"folder": str(folder)}
    error = _tag(text, "ErrorMessage")
    # The message often carries its own copy of the raw stack after "Stack:".
    error = re.split(r"\n\s*Stack:", error, maxsplit=1)[0]
    error = " ".join(error.split())
    stack = _tag(text, "CallStack")
    crash_type = _tag(text, "CrashType")
    if error:
        summary["error"] = error
    if crash_type:
        summary["type"] = crash_type
    if stack:
        frames: list[str] = []
        for ln in stack.splitlines():
            ln = ln.strip()
            # Unsymbolicated frames repeat the module name; one is enough.
            if ln and (not frames or frames[-1] != ln):
                frames.append(ln)
        summary["callstack"] = frames[:_STACK_LINES]
        if len(frames) > _STACK_LINES:
            summary["callstack_truncated"] = len(frames) - _STACK_LINES
    if ensures:
        summary["ensures_before"] = ensures

    summary.update(_log_facts(folder))
    return summary


def describe_loss(project_name: str, crash: dict[str, Any] | None) -> str:
    """One paragraph for a person: what happened and what to do."""
    who = project_name or "The Unreal editor"
    if crash is None:
        return (f"{who} is no longer running and left no crash report, so it was closed or "
                f"killed rather than crashed. Reopen the project; HullForge reconnects on the "
                f"next call with no restart needed.")
    error = crash.get("error") or "no error message was recorded"
    top = crash.get("callstack", [])[:3]
    where = f" Top of the call stack: {' | '.join(top)}." if top else ""
    tail = crash.get("log_tail") or []
    doing = f" The last thing it logged: {tail[-1][:300]}" if tail else ""
    return (f"{who} CRASHED: {error}.{where}{doing} The crash report is in {crash['folder']}. "
            f"Anything not saved before the crash is lost. Reopen the project; HullForge "
            f"reconnects on the next call with no restart needed.")
