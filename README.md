# HullForge MCP connector

The MCP server half of [HullForge](https://jigwright.com/hullforge), which lets
Claude drive a running Unreal Engine 5 editor.

**MIT licensed.** This repository is the connector only.

## What this is, and what it is not

HullForge is two programs.

| | |
| --- | --- |
| **This repository** | The connector. Speaks MCP to Claude over stdio, and a length-prefixed JSON protocol to the editor over loopback. MIT licensed. |
| **The editor plugin** | A separate, proprietary Unreal Engine plugin that holds every tool and does all the work. Licensed under its own EULA and distributed via Fab. Not in this repository. |

The connector contains **no Unreal Engine logic**. It is transport, framing,
session discovery, and the MCP surface. On its own it does nothing useful: with
no editor running it exposes exactly one tool, which tells you no editor is
running.

## Why it is a separate process

Embedding an MCP server directly in the C++ plugin would be one program instead
of two, which sounds simpler. It is not.

**The MCP surface changes far more often than the engine bindings.** Splitting
them means the protocol layer can be rebuilt in seconds without recompiling a
C++ module and restarting the editor.

**The editor's game thread is precious.** Everything touching a `UObject` must
run on it, and anything slow there freezes the editor. Keeping protocol,
framing, retries and transport outside means only the actual work is marshalled
onto that thread.

**Crash isolation.** A fault in protocol handling takes down a small external
process, not an editor with unsaved work in it.

## How it finds an editor

Nothing is configured. Each running editor advertises itself in a user-global
registry:

```
%LOCALAPPDATA%/HullForge/sessions/<project>-<pid>.json
```

Each entry holds a port, a per-session token, the project name and path, and a
process id. The connector scans that directory, filters by whether the process
is actually alive, and prefers the editor it is already connected to so a second
editor opening does not silently move the target mid-conversation.

It declares the MCP `listChanged` capability and watches that registry, so tools
appear when an editor opens and disappear when it closes, **without restarting
the client**. That matters because MCP servers are launched by the client: a
connector that starts before the editor would otherwise report an empty tool
list forever, since the client has no reason to ask again.

One tool, `hf_bridge_status`, is implemented here rather than in the editor. It
is therefore always available, so "no tools" is never a silent state.

## Reliability

Two things in here exist because the alternative loses work.

**Idempotency.** Every mutating call carries an `operation_id`. The connector
generates one per call and reuses it on transport retry, so a dropped connection
cannot apply the same change twice. The editor recognises a repeated id and
returns the original result.

**Honest failure states.** A call that definitely failed and a call whose
outcome is unknown are different answers and are reported differently. "Unknown"
means the connector stopped waiting, not that nothing happened, and the caller
is told to verify before retrying.

## Development

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest                       # unit tests, no editor needed
uv run pytest -m integration        # needs a running editor with the plugin
```

Building the distributable binary:

```
.\Build-Sidecar.ps1                 # Nuitka, for release
.\Build-Sidecar.ps1 -Fast           # PyInstaller, local iteration only
```

`Build-Sidecar.ps1` explains why release builds use Nuitka rather than
PyInstaller, and re-runs a leak audit after every build rather than assuming the
result.

## Installing

Most people should not build this. Install the packaged `.mcpb` bundle, which
contains a compiled binary and needs no Python.

In Claude Desktop: **Settings > Extensions > Advanced settings > Extension
Developer > Install Extension...**

Double-clicking the `.mcpb` also works where the OS has an association
registered for it, but the menu above is reliable everywhere.

Full instructions: [jigwright.com/hullforge/docs](https://jigwright.com/hullforge/docs)

## Privacy

The connector collects nothing, transmits nothing, and contacts no server. All
communication is over loopback. Full policy:
[jigwright.com/privacy](https://jigwright.com/privacy).

## Support

- Documentation: <https://jigwright.com/hullforge/docs>
- Issues in this repository, for the connector
- Email <support@jigwright.com> for anything about the editor plugin

## Licence

MIT, see [LICENSE](LICENSE). This applies to the connector in this repository
only. The HullForge editor plugin is a separate proprietary product under its
own terms.
