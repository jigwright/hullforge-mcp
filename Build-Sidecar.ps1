  HullForge sidecar, compiled to native code.
 
  WHY NOT PYINSTALLER
  -------------------
  PyInstaller is a container, not protection. It appends a compressed archive
  of .pyc bytecode to a bootstrap executable. That archive is fingerprinted
  (MEI cookie, pyimod01, _MEIPASS), module names sit in plaintext, and
  pyinstxtractor pulls the bytecode out in seconds. A decompiler then recovers
  near-original source: comments gone, logic and names intact.
 
  Nuitka compiles the Python to C and then to machine code. There is no
  bytecode archive to extract, because there is no bytecode. Reversing it means
  reversing compiled code in Ghidra or IDA, which is a different order of
  effort.
 
  HONEST LIMITS
  -------------
  This raises cost, it does not prevent. Anything that runs on someone else's
  machine can be reverse engineered given enough motivation. The realistic goal
  is that casual copying becomes impractical, and the licence covers the rest.
 
  The real moat is not the code anyway. Someone could recover every line and
  still not know why nested property paths matter for PCG, or that
  a landscape import blocks the game thread. That knowledge lives in
  the notes and the test suite, not the binary.
 
    .\Build-Sidecar.ps1                Nuitka, for release
    .\Build-Sidecar.ps1 -Fast          PyInstaller, for quick local iteration

[CmdletBinding()]
param(
    [switch] $Fast,
    [switch] $KeepSymbols
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

Push-Location $PSScriptRoot
try {
    if ($Fast) {
        Write-Host "PyInstaller build (fast, NOT protected)." -ForegroundColor Yellow
        & uv run pyinstaller hullforge-mcp.spec --noconfirm --distpath dist --workpath build
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
        Write-Host "`nBuilt dist\hullforge-mcp.exe" -ForegroundColor Green
        Write-Host "Do NOT ship this. Bytecode is extractable." -ForegroundColor Yellow
        return
    }

    Write-Host "Nuitka build. This takes several minutes." -ForegroundColor Cyan

    $args = @(
        'run', 'python', '-m', 'nuitka',

          One file the user can drop anywhere, same as before.
        '--onefile',
        '--assume-yes-for-downloads',

          Pull the whole package in rather than only the entry module, or the
          submodules end up as loose bytecode beside the binary.
        '--include-package=hullforge_mcp',

          The MCP SDK resolves parts of itself lazily, so static analysis
          misses them and the binary dies at first use rather than at build.
        '--include-package=mcp',
        '--include-package=anyio',
        '--include-package=pydantic',
        '--include-package=pydantic_core',

          console mode: the MCP stdio transport IS the console streams. A
          windowed build starts and then answers nothing at all.
        '--windows-console-mode=force',

        '--output-dir=dist-nuitka',
        '--output-filename=hullforge-mcp.exe',

        '--company-name=Jigwright',
        '--product-name=HullForge',
        '--file-version=0.1.0',
        '--product-version=0.1.0',
        '--file-description=HullForge MCP server for Unreal Engine',
        '--copyright=Copyright (c) 2026 Jigwright. All rights reserved.',

        'entrypoint.py'
    )

    if (-not $KeepSymbols) {
          Symbols hand a reverse engineer function names for free.
        $args += '--remove-output'
    }

    & uv @args
    if ($LASTEXITCODE -ne 0) { throw "Nuitka failed ($LASTEXITCODE)." }

    $exe = Join-Path $PSScriptRoot 'dist-nuitka\hullforge-mcp.exe'
    if (-not (Test-Path $exe)) { throw "No binary at $exe" }

    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "`nBuilt $exe ($size MB)" -ForegroundColor Green

      Verify the protection actually holds, rather than assuming it.
    Write-Host "`nChecking for PyInstaller-style exposure..." -ForegroundColor Cyan
    $text = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($exe))

    $leaks = @()
    foreach ($sig in @('_MEIPASS', 'pyimod01', 'PyInstaller', 'pyi-')) {
        if ($text -match [regex]::Escape($sig)) { $leaks += $sig }
    }
    if ($text -match 'hullforge_mcp\.(client|framing|server|session)') {
        $leaks += 'module names in plaintext'
    }

    if ($leaks) {
        Write-Host "  STILL EXPOSED: $($leaks -join ', ')" -ForegroundColor Red
    } else {
        Write-Host "  no extractable bytecode archive found" -ForegroundColor Green
    }
}
finally { Pop-Location }
