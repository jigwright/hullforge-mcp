# Copyright (c) 2026 Jigwright. All rights reserved.
"""Add up the connector's friction logs: where do agents fail, and repeat?

    python scripts/friction_report.py            # every session on this machine
    python scripts/friction_report.py --last 5   # the five most recent

Reads %LOCALAPPDATA%/HullForge/mcp/friction/*.jsonl (see friction.py) and
prints, per tool: calls, failures, repeated failures (the same call failing
again), and the most common first line of its errors. The top of this list
is the next thing to fix in the tools.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path,
                    default=Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HullForge" / "mcp" / "friction")
    ap.add_argument("--last", type=int, default=0, help="only the N most recent sessions")
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args()

    files = sorted(a.dir.glob("*.jsonl"))
    if a.last:
        files = files[-a.last:]
    calls: Counter[str] = Counter()
    fails: Counter[str] = Counter()
    repeats: Counter[str] = Counter()
    errors: dict[str, Counter[str]] = defaultdict(Counter)
    for f in files:
        for raw in f.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            tool = e.get("tool") or "?"
            calls[tool] += 1
            if not e.get("ok"):
                fails[tool] += 1
                if e.get("repeat", 0) >= 2:
                    repeats[tool] += 1
                first = (e.get("error") or "").strip().splitlines()[:1]
                errors[tool][first[0][:120] if first else ""] += 1

    total_calls, total_fails = sum(calls.values()), sum(fails.values())
    print(f"{len(files)} sessions, {total_calls} calls, {total_fails} failed "
          f"({100 * total_fails / max(total_calls, 1):.1f}%), {sum(repeats.values())} repeated")
    print(f"{'tool':48} {'calls':>6} {'fail':>5} {'rep':>4}  most common error")
    for tool, n in fails.most_common(a.top):
        common = errors[tool].most_common(1)[0][0] if errors[tool] else ""
        print(f"{tool:48} {calls[tool]:>6} {n:>5} {repeats[tool]:>4}  {common}")


if __name__ == "__main__":
    main()
