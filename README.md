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
session discovery, and the MCP surface: five fixed tools that reach every tool
the editor holds. On its own it does nothing useful: with no editor running,
`hf_bridge_status` tells you so and the other tools say the same.

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

It watches that registry, so an editor that opens after the client started is
picked up on the next call, **without restarting the client**, and a closed one
is dropped rather than leaving a dead connection behind.

`hf_bridge_status` is implemented here rather than in the editor, so it always
answers, and "no editor" is never a silent state.

## The tools

The editor offers about 3,000 tools. Listed in full, their names alone would
cost Claude Code about 78,000 tokens of context per session, and the Claude
clients don't pick up a tool list that changes mid-session. So the connector
lists five fixed tools that reach all of them, the same whether or not an
editor is open:

| Tool | Does |
| --- | --- |
| `hf_search_tools` | Finds tools from plain words; with only an area, lists that area; with nothing, lists the areas |
| `hf_tool_schema` | Exact arguments and description of up to 10 tools |
| `hf_read` | Calls a read-only tool |
| `hf_write` | Calls a tool that changes the project, verified by read-back as always |
| `hf_destroy` | Calls a destructive tool; refused unless `confirm` is true |

Search runs here, over the editor's catalog: ranked by name, first sentence,
area and description, with Unreal synonyms ("keyframe", "terrain", "VFX") and
each required argument's meaning shown with every hit.

Every call is checked against the tool's real schema before it reaches the
editor, and a refusal returns the schema. A destructive tool sent through
`hf_write`, or a write sent through `hf_read`, is refused rather than rerouted,
so permission rules on the three callers keep their meaning. Calling an editor
tool directly by name is refused too. The five tools carry
`anthropic/alwaysLoad`, so Claude Code loads them without a search step.

Measured with [hullforge-evals](https://github.com/jigwright/hullforge-evals)
(Sonnet, Claude Code headless), against listing every tool and against the
seven-area preset that came before this design:

| Suite | Every tool listed | Seven-area preset | Gateway |
| --- | ---: | ---: | ---: |
| 20 core tasks, passed | 39/40 | 39/40 | **40/40** |
| 17 tasks across other areas, passed | 21/21 | 4/20 | **20/20** |
| Startup context | 87k tokens | 28k | **21k** |
| Cold cost per task, core | $0.35 | $0.10 | **$0.06** |

### Looking up the engine API

A sixth fixed tool, `hf_api_lookup`, answers "which Unreal function or class does
this?" with no editor open: Blueprint-callable functions with their doc comments,
asset factories, subsystems, settings classes and HullForge's engine API notes.
Filter by `kind` or `editor_only`.

On first use the connector scans the headers of your own Unreal install (the
newest engine in the Epic launcher's install list, or `HULLFORGE_ENGINE` to pick
one) and caches the index under `%LOCALAPPDATA%\HullForge\mcp\api_index`. No
Epic code or text ships with the connector. The scan takes about 20 seconds; the
search embeddings (`bge-small-en-v1.5`, run locally) take a few minutes once.
Until they're ready, lookups use keyword search alone.

Search combines keywords, with Unreal synonyms, and the embeddings. On 30
questions written before tuning, the right entry was first 40% of the time and in
the top five 50%; see
[finding 11](https://github.com/jigwright/hullforge-evals/blob/main/FINDINGS.md).

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
