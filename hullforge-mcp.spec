# HullForge sidecar, frozen for distribution.
 
  Requiring buyers to install Python and uv is a real barrier for a Fab plugin.
  Its audience is Unreal developers, not Python developers, and "install these
  two things first" is where most of them stop.
 
  PyInstaller bundles the interpreter and every dependency into one .exe that
  ships inside the plugin. The two-process design survives - which matters,
  because hot-reloading the tool surface without a C++ rebuild is worth
  keeping - while the install burden disappears.
 
    uv run pyinstaller hullforge-mcp.spec --noconfirm
 
  Output: dist/hullforge-mcp.exe, self-contained.

import sys
from pathlib import Path

block_cipher = None

here = Path(SPECPATH)

a = Analysis(
      entrypoint.py, NOT server.py: PyInstaller runs the given script as
      __main__, and server.py uses relative imports. Freezing it directly
      builds fine and then dies on launch with "attempted relative import
      with no known parent package".
    [str(here / "entrypoint.py")],
    pathex=[str(here / "src")],
    binaries=[],
    datas=[],
      The MCP SDK resolves parts of itself lazily, so static analysis misses
      them and the frozen binary dies at first use rather than at build time.
    hiddenimports=[
        "mcp",
        "mcp.server",
        "mcp.server.lowlevel",
        "mcp.server.lowlevel.server",
        "mcp.server.stdio",
        "mcp.server.session",
        "mcp.types",
        "mcp.shared.context",
        "mcp_types",
        "anyio",
        "anyio._backends._asyncio",
        "pydantic",
        "pydantic_core",
        "hullforge_mcp",
        "hullforge_mcp.client",
        "hullforge_mcp.framing",
        "hullforge_mcp.server",
        "hullforge_mcp.session",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
      Test-only and dev-only weight. Excluding these roughly halves the binary.
    excludes=["pytest", "_pytest", "pluggy", "tkinter", "unittest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="hullforge-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            UPX-packed binaries trip antivirus heuristics
    upx_exclude=[],
    runtime_tmpdir=None,
      MUST stay True. The MCP stdio transport IS the console streams; a
      windowed build has no stdin/stdout and the server would appear to start
      and then never answer anything.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
