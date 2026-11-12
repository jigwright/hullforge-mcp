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

## Catalog mode

The editor advertises well over a hundred tools. Listing all of them costs the
model tens of thousands of tokens of context before it has done anything, and
most of a session touches two or three areas.

Set `HULLFORGE_CATALOG_MODE=1` and the tool list holds a small always-on core
plus whatever areas have been switched on:

```
HULLFORGE_CATALOG_MODE=1                    turn it on
HULLFORGE_CATALOG_AREAS=Materials,Sequencer start with those already listed
HULLFORGE_CATALOG_AREAS=@level              or a preset, for a kind of work
HULLFORGE_CATALOG_AREAS=@materials,Audio    presets and areas mix
HULLFORGE_TOOL_PRESET=@general              one setting: catalog mode on, with this preset
```

`HULLFORGE_TOOL_PRESET` is for places that offer one field, like the desktop
extension's settings. Blank means no preset; the two variables above win when set.

Presets (Core and Changes are always added; tool counts from UE 5.8):

| Preset | Tools | For |
| --- | ---: | --- |
| `@general` | 410 | Everyday editing: actors, assets, Blueprints, materials, lights |
| `@level` | 646 | Levels: landscape, foliage, meshes, splines, world partition |
| `@materials` | 367 | Materials, material instances, textures |
| `@blueprints` | 392 | Blueprint classes, components, input, game framework |
| `@gameplay` | 458 | GAS, gameplay tags, AI, StateTree, navigation |
| `@animation` | 591 | Animation, Control Rig, IK rigs, retargeting |
| `@cinematics` | 528 | Sequencer, cameras, movie rendering |
| `@vfx` | 377 | Niagara and its materials |
| `@audio` | 254 | Sound assets, MetaSounds |
| `@ui` | 352 | UMG, view models |
| `@pcg` | 499 | PCG graphs, splines, foliage, landscape |

`hf_list_tool_areas` lists the presets with live counts, and warns about a preset
or area name that matches nothing, instead of quietly listing fewer tools.

`hf_list_tool_areas` shows every area with its tool count and whether it is
listed. `hf_enable_tool_areas` and `hf_disable_tool_areas` change the set, and
the tool list updates immediately through the same `tools/list_changed`
mechanism that makes tools appear when an editor opens. The `Core` area is
always on and cannot be disabled.

**It is off by default and that is deliberate.** The whole argument for this
connector is that the tool list is the truth about what the editor will
dispatch, and a mode that silently hides two thirds of it should be asked for
rather than assumed. `hf_list_tool_areas` is listed either way, so the mode is
discoverable without being imposed. A tool from an area that is not listed is
refused rather than quietly run, for the same reason: a hidden tool that works
anyway would make the list advice.

**In Claude Code, preset the areas.** Switching areas on mid-session depends on
the client re-reading the tool list after `tools/list_changed`, and Claude Code
never does, so areas enabled that way never become callable there. Areas named in
`HULLFORGE_CATALOG_AREAS` are listed from startup, which Claude Code does see, so
pick a preset for the work (`@general` if unsure).
Measured with [hullforge-evals](https://github.com/jigwright/hullforge-evals) on
20 tasks: `@general` (the same seven areas) passed the same 39 of 40 runs as the
full list, with 88k tokens of context per run instead of 265k, and $2.60 total
instead of $8.34 (API-equivalent). With no preset, leave catalog mode off in
Claude Code; its tool search handles the full list.

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
