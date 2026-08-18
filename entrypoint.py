"""Frozen-build entry point.

Exists because PyInstaller runs whatever script it is given as ``__main__``.
Pointing it straight at ``hullforge_mcp/server.py`` makes that module's
relative imports (``from .client import ...``) fail with "attempted relative
import with no known parent package" - the binary builds cleanly and then dies
on first launch.

Freezing this launcher instead keeps the package a package.
"""

from __future__ import annotations

from hullforge_mcp.server import main

if __name__ == "__main__":
    main()
