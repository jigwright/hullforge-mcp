# HullForge connector: security model

HullForge lets an AI model change a running Unreal project. This page says what
stops it doing harm, and what you should set up.

## Who approves what

| Call | Who approves |
|---|---|
| `hf_read`, `hf_search_tools`, `hf_tool_schema`, `hf_api_lookup`, `hf_bridge_status` | Nobody: they change nothing. |
| `hf_write`, `hf_batch`, `hf_run_code` | Your client's permission rules. Every write is verified by reading it back, has a checkpoint to roll back to, and can be previewed (`preview=true`). |
| `hf_destroy` (delete, overwrite, revert) | **You.** When your client supports MCP elicitation (Claude Code does), HullForge asks you directly; the model cannot answer for you. On the 2026-07-28 protocol the question comes back as `input_required` and your client asks you before calling again; the answer is sealed to that one call. Clients without elicitation fall back to `confirm=true`, which the model can set, so for those keep `hf_destroy` on "ask" in the client. |
| `hf_source_add_dependency`, `hf_source_add_module` (change how the project builds) | **You**, the same way. |
| `hf_restart_editor` | **You.** It saves first and never kills the editor. |

Recommended Claude Code permissions: `docs/claude-code-settings.json`.

## Code

* Console commands, Python and other "run this string" channels are not
  exposed. C++ goes through the source tools only.
* `hf_run_code` runs a Starlark script, not Python: the language has no
  import, `while`, files, network, clock or recursion. It runs in its own
  process, capped at 512 MB and stopped past its time limit, and can reach
  nothing but HullForge calls, each checked exactly as a model's
  `hf_read`/`hf_write` call is. Destroying tools and tools that need your
  approval are refused inside a script.
* Source tools write only inside the project's own source folders (never
  engine source, other plugins or HullForge itself), refuse paths through
  symbolic links or junctions, back every file up first
  (`hf_source_restore`), and refuse `.Build.cs` and `.Target.cs` as text.
  Module changes go through `hf_source_add_dependency` / `hf_source_add_module`,
  which you approve.
* Every compile runs `hf_source_risk_scan` first and refuses while a finding
  (process launch, network, file system and registry access) is
  not accepted by you.
* Source edits are off until you turn on **Allow source edits** for the project.

## Transport

* The editor side listens on 127.0.0.1 only, rejects any non-loopback peer,
  and requires a random per-launch token as the first frame. The token is in a
  per-user session file.
* The connector speaks MCP over stdio only; it opens no network port.

## Prompt injection

Text that comes back from the project (asset names, descriptions, log lines,
source comments) is data. The server instructions say so, and a result whose
text reads like instructions to the model is marked `_untrusted`. Whatever the
model concludes, the stage lock (no writes during design and plan), the risk
tiers and your approvals above are enforced by the connector, not by the model.

## Pinning the tool definitions

`hf_bridge_status` reports `definitions_sha256`: a hash of the server
instructions and every tool definition the model sees. Note it after you review
a release; if it changes without an upgrade, something changed the server.

## Supply chain

Dependencies are pinned in `uv.lock`. Install from a tagged release.

## Reporting

support@jigwright.com
