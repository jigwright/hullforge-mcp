"""HullForge agent evals: does a task get done, how reliably, and at what cost.

Runs each task k times in Claude Code (headless, `claude -p`) against the open
Unreal project, then checks the result OBJECTIVELY through the connector
itself, never by trusting what the agent said. Reports, per configuration:

  pass^k        every one of the k runs passed (tau-bench's reliability metric)
  pass@1        share of runs that passed
  turns         model round-trips per run (each re-reads the whole context)
  calls         tool calls per run, and tool errors per run
  tokens        fresh input, cache-read input and output per run
  seconds       wall clock per run

Usage (from the connector repo, with the editor open on the project):
  uv run python evals/run_evals.py --project C:/HFDemo/ArenaDemo -k 4
  uv run python evals/run_evals.py --project ... --config full --config lean --task pickup

Configurations: 'lean' is scripts/hf-claude.ps1's launch (no built-in tools,
only HullForge, HullForge's system prompt); 'full' is a default Claude Code
launch with HullForge allowed. Results go to evals/results/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))

from hullforge_mcp.server import HullForgeServer  # noqa: E402

ALLOWED = ",".join(f"mcp__hullforge__{t}" for t in (
    "hf_bridge_status", "hf_search_tools", "hf_tool_schema", "hf_api_lookup",
    "hf_read", "hf_write", "hf_batch", "hf_destroy"))


def launch_args(config: str) -> list[str]:
    mcp = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "HullForge" / "claude-mcp.json"
    mcp.parent.mkdir(parents=True, exist_ok=True)
    mcp.write_text(json.dumps({"mcpServers": {"hullforge": {
        "type": "stdio", "command": "uv",
        "args": ["run", "--directory", str(REPO).replace("\\", "/"), "hullforge-mcp"]}}}), encoding="utf-8")
    base = ["--output-format", "stream-json", "--verbose", "--allowedTools", ALLOWED]
    if config == "lean":
        return base + ["--tools", "", "--strict-mcp-config", "--mcp-config", str(mcp),
                       "--system-prompt-file", str(REPO / "scripts" / "hf_system.md")]
    if config == "full":
        return base
    raise SystemExit(f"unknown config {config}")


def run_claude(prompt: str, config: str, project: Path, model: str | None, timeout: int) -> dict[str, Any]:
    cmd = ["claude", "-p", prompt, *launch_args(config)]
    if model:
        cmd += ["--model", model]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=project, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, shell=(os.name == "nt"))
    stats: dict[str, Any] = {"seconds": round(time.monotonic() - t0, 1), "calls": 0, "tool_errors": 0,
                             "turns": 0, "fresh_in": 0, "cache_read": 0, "cache_write": 0, "out": 0}
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") or {}
        if ev.get("type") == "assistant" and msg.get("id") not in seen:
            seen.add(msg.get("id"))
            stats["turns"] += 1
            u = msg.get("usage") or {}
            stats["fresh_in"] += u.get("input_tokens", 0) or 0
            stats["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            stats["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
            stats["out"] += u.get("output_tokens", 0) or 0
        for c in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
            if c.get("type") == "tool_use":
                stats["calls"] += 1
            if c.get("type") == "tool_result" and c.get("is_error"):
                stats["tool_errors"] += 1
        if ev.get("type") == "result":
            stats["final"] = str(ev.get("result") or "")[:500]
    if proc.returncode != 0:
        stats["exit"] = proc.returncode
        stats["stderr"] = proc.stderr[-500:]
    return stats


def _get(value: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _ok(value: Any, op: str, want: Any) -> bool:
    if op == "contains":
        return str(want) in (json.dumps(value) if not isinstance(value, str) else value)
    if op == "==":
        return value == want
    if op == "exists":
        return value is not None
    if op in (">=", "<="):
        try:
            return float(value) >= float(want) if op == ">=" else float(value) <= float(want)
        except (TypeError, ValueError):
            return False
    raise ValueError(op)


async def check(srv: HullForgeServer, task: dict[str, Any]) -> tuple[bool, list[str]]:
    notes: list[str] = []
    passed = True
    for c in task.get("checks", []):
        r = await srv._gateway_invoke("hf_read", {"tool": c["tool"], "args": c.get("args", {})})
        body = r.structured_content if not r.is_error else None
        got = _get(body, c.get("path", "")) if body is not None else None
        good = body is not None and _ok(got, c.get("op", "contains"), c.get("value"))
        passed &= good
        notes.append(f"{'PASS' if good else 'FAIL'} {c['tool']} {c.get('path', '')} {c.get('op', 'contains')} "
                     f"{c.get('value')!r} (got {json.dumps(got)[:120] if body is not None else 'error: ' + r.content[0].text[:160]})")
    return passed, notes


async def cleanup(srv: HullForgeServer, task: dict[str, Any]) -> None:
    for c in task.get("cleanup", []):
        caller = c.get("caller", "hf_destroy")
        args = {"tool": c["tool"], "args": c.get("args", {})}
        if caller == "hf_destroy":
            args["confirm"] = True
        await srv._gateway_invoke(caller, args)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--config", action="append")
    ap.add_argument("--task", action="append")
    ap.add_argument("--model")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()
    project = Path(a.project)
    tasks = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((HERE / "tasks").glob("*.json"))]
    if a.task:
        tasks = [t for t in tasks if t["id"] in a.task]
    srv = HullForgeServer(str(project))
    await srv._refresh_catalog()
    results: list[dict[str, Any]] = []
    for config in a.config or ["lean"]:
        for task in tasks:
            runs = []
            for i in range(a.k):
                await cleanup(srv, task)
                stats = run_claude(task["prompt"], config, project, a.model, a.timeout)
                await srv._refresh_catalog()
                stats["passed"], stats["checks"] = await check(srv, task)
                runs.append(stats)
                print(f"{config} {task['id']} run {i + 1}/{a.k}: {'PASS' if stats['passed'] else 'FAIL'} "
                      f"turns={stats['turns']} calls={stats['calls']} errors={stats['tool_errors']} "
                      f"cache_read={stats['cache_read']} {stats['seconds']}s", flush=True)
            await cleanup(srv, task)

            def mean(key: str) -> float:
                return round(statistics.mean(r[key] for r in runs), 1)
            results.append({"config": config, "task": task["id"], "k": a.k,
                            "pass^k": all(r["passed"] for r in runs),
                            "pass@1": round(sum(r["passed"] for r in runs) / len(runs), 2),
                            **{f"mean_{x}": mean(x) for x in ("turns", "calls", "tool_errors", "fresh_in",
                                                              "cache_read", "out", "seconds")},
                            "runs": runs})
    out = HERE / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print("\nconfig  task                pass^k pass@1 turns calls errors cache_read seconds")
    for r in results:
        print(f"{r['config']:<7} {r['task']:<19} {str(r['pass^k']):<6} {r['pass@1']:<6} {r['mean_turns']:<5} "
              f"{r['mean_calls']:<5} {r['mean_tool_errors']:<6} {r['mean_cache_read']:<10} {r['mean_seconds']}")
    print(f"\n{out}")
    if srv._client is not None:
        await srv._client.close()


if __name__ == "__main__":
    asyncio.run(main())
