# Copyright (c) 2026 Jigwright. All rights reserved.
"""Where agents struggle: one log of tool-call outcomes per connector session.

A rehearsal agent building a small pickup spent a dozen calls on refusals,
retries and workarounds, and the only way anyone found out was by reading
its transcript. The connector sees every call already, so it writes each
outcome here: which tool, whether it failed, the first part of the error,
and how many times that exact call had failed before. The next gap in the
tools is then a count, not a hunch.

One JSON line per call in
    %LOCALAPPDATA%/HullForge/mcp/friction/<started>-<pid>.jsonl
next to the session registry, and a running summary that hf_bridge_status
returns. Arguments are NOT logged - they can hold project paths and text the
user typed - only their names. scripts/friction_report.py adds sessions up.

Best effort throughout: a log that cannot be written never fails a call.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any


class FrictionLog:
    def __init__(self, directory: Path | None) -> None:
        self._dir = directory
        self._path: Path | None = None
        self.calls = 0
        self.failed = 0
        self.repeated = 0
        self._failures_by_tool: Counter[str] = Counter()
        self._started = time.strftime("%Y%m%d-%H%M%S")

    @property
    def path(self) -> Path | None:
        return self._path

    def record(self, *, caller: str, tool: str, ok: bool, error: str = "",
               repeat: int = 0, arg_names: list[str] | None = None) -> None:
        self.calls += 1
        if not ok:
            self.failed += 1
            self._failures_by_tool[tool or "?"] += 1
            if repeat >= 2:
                self.repeated += 1
        line = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "caller": caller,
            "tool": tool,
            "ok": ok,
            "args": sorted(arg_names or []),
        }
        if not ok:
            line["error"] = error[:400]
            line["repeat"] = repeat
        self._write(line)

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failed": self.failed,
            "repeated_failures": self.repeated,
            "top_failing": [{"tool": t, "failures": n}
                            for t, n in self._failures_by_tool.most_common(5)],
            "log": str(self._path) if self._path else None,
        }

    def _write(self, line: dict[str, Any]) -> None:
        if self._dir is None:
            return
        try:
            if self._path is None:
                self._dir.mkdir(parents=True, exist_ok=True)
                self._path = self._dir / f"{self._started}-{os.getpid()}.jsonl"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass  # best effort: the call matters more than its log line
