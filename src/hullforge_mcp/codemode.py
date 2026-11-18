"""Code mode: the agent writes a short Starlark script instead of a run of
tool calls, and only the script's result comes back.

hf_batch covers a fixed list of steps. A script covers the rest: loops over
what a read returned, arithmetic, building names, filtering a big result
down to the few fields that matter, all without a model turn in between and
without the intermediate results entering the context.

The safety comes from three layers:
  * the language: Starlark (starlark-pyo3) has no import, while, file,
    network, clock or randomness, and no recursion;
  * the process: the script runs in its own worker, capped in memory and
    killed past its time, so a runaway script cannot take the connector or
    the editor with it;
  * the gateway: every tool call the script makes runs through the same
    path as a model's hf_read/hf_write call, schema check, stage lock and
    all; destroying tools and tools that need the user's approval are
    refused, as they are in hf_batch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

WORKER = Path(__file__).with_name("codemode_worker.py")

MAX_CODE_CHARS = 20_000
MAX_CALLS = 200
DEFAULT_SECONDS = 10.0
MAX_SECONDS = 60.0
# Wall time for the whole run, tool calls included (a compile can be slow).
MAX_WALL_SECONDS = 1800.0
MEMORY_BYTES = 512 * 1024 * 1024
MAX_LINE = 8 * 1024 * 1024

# One tool call from the script: (kind, payload) -> (ok, value or message).
Handler = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, Any]]]


def available() -> str | None:
    """None when code mode can run here, else why not."""
    try:
        import starlark  # noqa: F401
    except ImportError:
        return "the starlark-pyo3 package is not installed"
    return None if WORKER.is_file() else "the code-mode worker is missing"


def _limit_posix() -> None:  # pragma: no cover - runs in the child
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))


def _limit_windows(pid: int) -> Any:
    """A job object that caps the worker's memory and kills it when the
    handle closes. Returned so it lives as long as the run."""
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["ProcessMemoryLimit"] = MEMORY_BYTES
    info["BasicLimitInformation"]["LimitFlags"] = (
        win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | win32job.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
    try:
        win32job.AssignProcessToJobObject(job, handle)
    finally:
        win32api.CloseHandle(handle)
    return job


async def run(code: str, handler: Handler, *, seconds: float = DEFAULT_SECONDS) -> dict[str, Any]:
    """Run one script. Returns {"result", "calls", "logs"} or {"error", ...}."""
    if not isinstance(code, str) or not code.strip():
        return {"error": "'code' must be a non-empty Starlark script."}
    if len(code) > MAX_CODE_CHARS:
        return {"error": f"'code' is longer than {MAX_CODE_CHARS} characters; split the work."}
    why = available()
    if why:
        return {"error": f"Code mode is not available: {why}. Use hf_batch instead."}
    seconds = max(0.5, min(float(seconds or DEFAULT_SECONDS), MAX_SECONDS))

    kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        kwargs["preexec_fn"] = _limit_posix
    else:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "PATH", "TEMP", "TMP")}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(WORKER),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env, limit=MAX_LINE, **kwargs)
    job = None
    calls = 0
    try:
        if sys.platform == "win32":
            job = _limit_windows(proc.pid)
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps({"code": code, "seconds": seconds}) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        async def converse() -> dict[str, Any]:
            nonlocal calls
            while True:
                line = await proc.stdout.readline()
                if not line:
                    await proc.wait()
                    return {"error": "The script was stopped: it used more than "
                                     f"{MEMORY_BYTES // (1024 * 1024)} MB. A script keeps every value it "
                                     "makes until it ends, so a long loop of arithmetic runs out too; do "
                                     "less per script. Tool calls it made before this stayed applied.", "killed": True}
                msg = json.loads(line)
                if "call" not in msg:
                    return msg
                calls += 1
                if calls > MAX_CALLS:
                    reply: dict[str, Any] = {"raise": f"a script makes at most {MAX_CALLS} tool calls"}
                else:
                    kind = str(msg.pop("call"))
                    try:
                        ok, value = await handler(kind, msg)
                    except Exception as exc:  # noqa: BLE001 - a failed call is the script's to see
                        ok, value = False, f"{type(exc).__name__}: {exc}"
                    if ok:
                        reply = {"value": value}
                    elif msg.get("check", True) is False:
                        reply = {"value": {"error": value}}
                    else:
                        reply = {"raise": str(value)}
                proc.stdin.write((json.dumps(reply, default=str) + "\n").encode("utf-8"))
                await proc.stdin.drain()

        try:
            out = await asyncio.wait_for(converse(), timeout=MAX_WALL_SECONDS)
        except asyncio.TimeoutError:
            return {"error": f"The run took longer than {int(MAX_WALL_SECONDS)} s and was stopped.",
                    "calls": calls}
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        # Read the pipe to its end so its transport closes now, not at
        # garbage collection (an 'unclosed transport' warning on Windows).
        if proc.stdout is not None:
            try:
                await asyncio.wait_for(proc.stdout.read(), timeout=2.0)
            except Exception:  # noqa: BLE001 - only tidying up
                pass
        if job is not None:
            import win32api
            win32api.CloseHandle(job)

    out["calls"] = calls
    if "done" in out:
        out["result"] = out.pop("done")
    if out.get("timed_out"):
        out["error"] = (f"The script used more than {seconds:g} s of its own time (tool calls do not "
                        f"count) and was stopped. " + str(out.get("error") or ""))
    if not out.get("logs"):
        out.pop("logs", None)
    out.pop("timed_out", None)
    return out
