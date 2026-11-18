"""The code-mode worker: runs one Starlark script and nothing else.

Started by codemode.py as its own process, so a script that runs away (CPU,
memory, a crash inside the interpreter) takes only this process down. It
talks to its parent in JSON lines: the parent sends the job, the worker
sends each tool call and waits for its answer, then sends the result.

The script can reach nothing but the functions below. Starlark has no
import, no while, no file, network, clock or randomness, and recursion is
refused; this module adds only the calls that go back to HullForge, where
every one is checked as if the model had made it.
"""

from __future__ import annotations

import json
import os
import sys
import time

# Run as a file (python -I codemode_worker.py), never imported by the server.


def _plain(value: object) -> object:
    """Starlark values come back as Python ones; anything else as text."""
    return json.loads(json.dumps(value, default=str))


def _short_error(exc: BaseException) -> str:
    """The interpreter's message without its ASCII art."""
    text = str(exc)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    keep: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("error:") or s.startswith("* ") or s.startswith("-->"):
            keep.append(s.replace("error: RuntimeError: ", "error: "))
    return "\n".join(keep) or (lines[-1] if lines else type(exc).__name__)


def main() -> int:
    proto_in = sys.stdin.buffer
    # The protocol keeps the real stdout; anything else that writes there
    # (nothing should) goes nowhere instead of corrupting it.
    proto_out = os.fdopen(os.dup(1), "wb", buffering=0)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)

    def send(obj: dict) -> None:
        proto_out.write((json.dumps(obj, default=str) + "\n").encode("utf-8"))

    line = proto_in.readline()
    if not line:
        return 2
    job = json.loads(line)

    import starlark as sl  # after the job arrives, so the parent can cap us first

    budget = float(job.get("seconds", 10.0))
    spent_waiting = 0.0
    started = time.monotonic()
    logs: list[str] = []

    def over_time() -> bool:
        return time.monotonic() - started - spent_waiting > budget

    def ask(kind: str, payload: dict) -> object:
        nonlocal spent_waiting
        send({"call": kind, **payload})
        t0 = time.monotonic()
        reply = proto_in.readline()
        # Time in HullForge (a compile, a playtest) is not the script's time.
        spent_waiting += time.monotonic() - t0
        if not reply:
            os._exit(3)
        answer = json.loads(reply)
        if "raise" in answer:
            raise RuntimeError(answer["raise"])
        return answer.get("value")

    def read(tool: str, args: dict | None = None, check: bool = True) -> object:
        return ask("read", {"tool": tool, "args": _plain(args or {}), "check": bool(check)})

    def write(tool: str, args: dict | None = None, check: bool = True) -> object:
        return ask("write", {"tool": tool, "args": _plain(args or {}), "check": bool(check)})

    def search(query: str, limit: int = 5) -> object:
        return ask("search", {"query": str(query), "limit": int(limit)})

    def log(*values: object) -> None:
        if len(logs) < 100:
            logs.append(" ".join(v if isinstance(v, str) else json.dumps(_plain(v)) for v in values)[:1000])
        return None

    module = sl.Module()
    for name, fn in (("read", read), ("write", write), ("search", search), ("log", log)):
        module.add_callable(name, fn)
    globals_ = sl.Globals.extended_by([sl.LibraryExtension.Json, sl.LibraryExtension.Map,
                                       sl.LibraryExtension.Filter])
    try:
        ast = sl.parse("script.star", str(job.get("code") or ""), sl.Dialect.extended())
    except sl.StarlarkError as exc:
        send({"error": _short_error(exc), "phase": "parse", "logs": logs})
        return 0
    try:
        result = sl.eval_with(sl.EvalOptions(check_cancelled=over_time, max_callstack_size=200),
                              module, ast, globals_)
        send({"done": _plain(result.value), "logs": logs})
    except BaseException as exc:  # noqa: BLE001 - every failure goes back as a message
        send({"error": _short_error(exc), "timed_out": over_time(), "logs": logs})
    return 0


if __name__ == "__main__":
    sys.exit(main())
