# Copyright (c) 2026 Jigwright. All rights reserved.
"""MCP server for HullForge.

Serves a tool catalog that is fetched from the running editor rather than
declared here. Schemas live next to their handlers in C++ and travel over the
wire via hf_describe_tools, so the MCP catalog cannot drift from what the
editor will actually dispatch.

Built on the lowlevel Server rather than MCPServer: the high-level API derives
input schemas from Python function signatures, which would mean re-declaring
every schema on this side and inventing exactly the drift we are avoiding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server

from .client import BridgeClient, BridgeError, ToolFailed, ToolOutcomeUnknown
from . import api_index as _api_index
from . import gateway as _gateway
from . import focus as _focus
from . import crash as _crash
from . import playbooks as _playbooks
from .friction import FrictionLog
from mcp.shared.exceptions import MCPError
from .session import (
    SessionError,
    SessionInfo,
    discover,
    discover_all,
    process_is_alive,
    session_registry_dir,
)

# Window captures that only show a current frame while the editor is in front.
_FOCUS_FOR_CAPTURE = frozenset({"hf_screenshot"})

log = logging.getLogger("hullforge")

# Tools that exist to serve the bridge itself. The model never needs to see
# them: the catalog is already expanded into real MCP tools.
_INTERNAL_TOOLS = {"hf_describe_tools"}

# How often to look for an editor appearing or going away.
_WATCH_INTERVAL_SECONDS = 1.0

# The area the connector's own synthetic tools belong to.
_ALWAYS_ON_AREA = "Core"

# Where a tool's area travels to any client that wants it. MCP's _meta is the
# sanctioned extension point and a domain-prefixed key is the convention.
_AREA_META_KEY = "jigwright.com/area"


# Always present, even with no editor running. Without this, "no editor" shows
# up as an empty tool list, which is silent and baffling - the user has no way
# to tell a broken install from an editor they forgot to open.
_BRIDGE_STATUS_TOOL = types.Tool(
    # Core, like the editor tools it sits beside, so anything reading
    # _meta to group tools gets a complete answer and needs no special
    # case for the ones the sidecar synthesises.
    # Preloaded in Claude Code like the gateway tools: a rehearsal agent
    # spent three calls finding it (a schema lookup and two ToolSearch loads).
    meta={_AREA_META_KEY: _ALWAYS_ON_AREA, "anthropic/alwaysLoad": True},
    name="hf_bridge_status",
    title="Bridge Status",
    description=(
        "Whether HullForge is connected to a running Unreal editor, and what to "
        "do if not. This tool is always available even when no editor is open. "
        "Once an editor is running, the other HullForge tools reach it without "
        "restarting the client."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    annotations=types.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True
    ),
)


# Words that must not be title-cased naively when building a display title.
_ACRONYMS = {
    "pcg": "PCG",
    "id": "ID",
    "url": "URL",
    "ui": "UI",
    "uv": "UV",
    "lod": "LOD",
    # Plural too. Without it the LODs area shipped a page reading "Audit Lods"
    # directly above "Set LOD Group", because the singular was in the table and
    # the plural was not. Any acronym that appears in a plural tool name needs
    # both forms here.
    "lods": "LODs",
    "scc": "SCC",
}

# Tools that can destroy work that is not recoverable by undo. Everything else
# that mutates is still a write, but calling a material-instance creation
# "destructive" is simply false and trains the reader to ignore the flag.
_DESTRUCTIVE = {
    "hf_delete_asset",
    "hf_delete_actor",
    "hf_delete_blueprint_node",
    "hf_rename_asset",     # breaks anything referencing the old path
    "hf_open_level",       # discards unsaved changes
    # Rewrites and SAVES every package that referenced a redirector, then
    # deletes the redirectors. Nothing about it is undoable, and it is the only
    # tool here that changes assets the caller did not name.
    "hf_fix_redirectors",
}

# Deliberately NOT flagged, recorded so the question does not get reopened.
# hf_remove_widget and hf_reparent_widget both destroy configuration, and both
# do it inside an editor transaction that an undo really does reverse. Flagging
# every mutation that loses something would make the flag meaningless, and a
# reader who learns to ignore it also ignores it on hf_delete_asset.
#
# Same for hf_set_sequence_playback_range and hf_set_sequence_track_muted. Both
# change one value on one object, both go through Modify() first, and both are
# reversed by a single undo. Narrowing a playback range can leave sections
# outside it and no longer evaluating, which looks destructive and is not: the
# sections are untouched and widening the range brings them back. The tool says
# so in not_checked rather than borrowing a flag that means something stronger.


def _display_title(tool_name: str) -> str:
    """'hf_set_material_parameter' -> 'Set Material Parameter'.

    The directory requires every tool to carry a title. Deriving it from the
    name keeps one source of truth: adding a tool in C++ should not also
    require remembering to write a title by hand, which is exactly the kind of
    metadata that rots.
    """
    stem = tool_name[3:] if tool_name.startswith("hf_") else tool_name
    words = [_ACRONYMS.get(w, w.capitalize()) for w in stem.split("_") if w]
    return " ".join(words) or tool_name


class HullForgeServer:
    def __init__(self, project_dir: str | os.PathLike[str] | None = None) -> None:
        self._project_dir = project_dir
        self._client: BridgeClient | None = None
        self._catalog: list[types.Tool] = []
        # Area name per tool, from the editor. Nothing here guesses it: a tool
        # with no area is filed under Uncategorised by the editor.
        self._areas: dict[str, str] = {}
        self._mutating: set[str] = set()
        self._session_key: tuple[int, str] | None = None
        self._editor_pid: int = 0
        # The editor this connector last reached, kept after it goes away so
        # its loss can be explained: which project, which process, and since
        # when, so only a crash report written after that counts.
        self._last_editor: dict[str, Any] | None = None
        self._loss: dict[str, Any] | None = None
        self._mcp_session: Any = None
        # The gateway's view of the editor's catalog: searched, validated
        # against, never listed. See gateway.py.
        self._index = _gateway.Index()
        # The engine API lookup. Built from the installed engine on first use,
        # in a thread, so it never blocks the editor tools.
        self._api = _api_index.LazyApiIndex()
        # Every gateway call's outcome, for this session: what failed, and
        # how often the same call failed again. See friction.py.
        self._friction = FrictionLog(session_registry_dir().parent / "mcp" / "friction")
        # The workflow stage last loaded. Design and plan change nothing, and
        # saying so in the playbook was not enough: an agent asked in a design
        # to "compile it and place one" built the lot. While one of them is
        # the stage, writes are refused; loading build or verify lifts it.
        self._stage: str | None = None
        self._failures: dict[tuple[str, str], tuple[int, str]] = {}
        # Long notes a tool repeats on every call (not_checked, freshness...)
        # go out once per session; see _shrink_repeats.
        self._said: dict[str, str] = {}
        # Set per call from 'verbose'; a successful write comes back short unless it is.
        self._verbose = False
        self.server: Server = Server(
            "hullforge",
            version="0.1.0",
            instructions=_gateway.INSTRUCTIONS,
        )
        # Use the concrete params models directly. Reading them off
        # Request.model_fields["params"].annotation looks tidier but yields a
        # union for optional params (PaginatedRequestParams | None), and the
        # SDK calls .model_validate on whatever it is given - so a union fails
        # at request time with a confusing 'types.UnionType has no attribute
        # model_validate', not at registration.
        self.server.add_request_handler(
            "tools/list", types.PaginatedRequestParams, self._handle_list_tools,
        )
        self.server.add_request_handler(
            "tools/call", types.CallToolRequestParams, self._handle_call_tool,
        )
        # The workflow playbooks. Fixed text, served with or without an editor.
        self.server.add_request_handler(
            "prompts/list", types.PaginatedRequestParams, self._handle_list_prompts,
        )
        self.server.add_request_handler(
            "prompts/get", types.GetPromptRequestParams, self._handle_get_prompt,
        )

    # -- bridge ------------------------------------------------------------

    async def _ensure_connected(self) -> BridgeClient:
        if self._client is not None and self._client.connected:
            return self._client

        info = self._pick_session()                 # raises SessionError
        client = BridgeClient(info)
        await client.connect()                      # raises BridgeError
        self._client = client
        self._session_key = (info.port, info.token)
        self._editor_pid = int(getattr(info, "pid", 0) or 0)
        self._last_editor = {
            "pid": self._editor_pid,
            "project": info.project_name,
            "project_dir": info.project_dir,
            "connected_at": time.time(),
        }
        self._loss = None
        log.info("connected to %s on port %d (v%s)",
                 info.project_name or "editor", info.port, client.server_version)
        return client

    def _editor_loss(self) -> dict[str, Any] | None:
        """Why the editor last connected to is gone, or None while it lives.

        Only answers when the process is really gone: a dropped socket to a
        live editor is a network problem, not a crash, and is not reported as
        one.
        """
        if self._loss is not None:
            return self._loss
        last = self._last_editor
        if not last or not last["pid"] or process_is_alive(last["pid"]):
            return None
        report = _crash.find_latest_crash(last["project_dir"], last["connected_at"])
        loss = {
            "project": last["project"],
            "pid": last["pid"],
            "crashed": report is not None,
            "crash": report,
            "summary": _crash.describe_loss(last["project"], report),
        }
        # A crash report can land a moment after the process is gone, so only
        # a found report is final; "closed" is looked at again next time.
        if report is not None:
            self._loss = loss
            log.warning("editor %s (pid %d) crashed: %s", last["project"], last["pid"],
                        report.get("error", "?"))
        return loss

    def _with_loss(self, text: str) -> types.CallToolResult:
        """An error result, with the reason the editor went away when it did."""
        loss = self._editor_loss()
        if loss is None:
            return _error(text)
        result = _error(f"{text}\n\n{loss['summary']}")
        result.structured_content = {"editor_lost": loss}
        return result

    def _pick_session(self) -> SessionInfo:
        """The editor to talk to.

        Prefers the one already connected to, so a second editor opening does
        not silently move the target out from under an in-flight conversation.
        """
        sessions = discover_all(self._project_dir)
        if not sessions:
            raise SessionError(
                "No running Unreal editor with the HullForge plugin was found. "
                "Open your project; HullForge connects on the next call, with no "
                "need to restart this client."
            )

        if self._session_key is not None:
            for info in sessions:
                if (info.port, info.token) == self._session_key:
                    return info

        return sessions[0]

    async def _refresh_catalog(self) -> list[types.Tool]:
        client = await self._ensure_connected()
        described = await client.call("hf_describe_tools")

        tools: list[types.Tool] = []
        mutating: set[str] = set()
        areas: dict[str, str] = {}
        for entry in described.get("tools", []):
            name = entry.get("name", "")
            if not name or name in _INTERNAL_TOOLS:
                continue

            schema = entry.get("input_schema")
            if not isinstance(schema, dict) or not schema:
                # A tool with no usable schema would force the model to guess.
                # Surface it rather than shipping an untyped tool.
                log.warning("tool %r has no usable input_schema; skipping", name)
                continue

            mutating_flag = bool(entry.get("mutating"))
            if mutating_flag:
                mutating.add(name)

            areas[name] = str(entry.get("area") or "Uncategorised")

            # destructive_hint means "can destroy work", not "writes".
            # Flagging every mutation destructive would make the flag
            # meaningless - creating a material instance is a write, not a
            # hazard - and a reader who learns to ignore it also ignores it on
            # hf_delete_asset.
            #
            # THE EDITOR DECIDES. A tool declares bHazardous next to its
            # handler and hf_describe_tools sends it down with the rest. The
            # set below is kept only to catch a disagreement: two copies of one
            # fact in two repositories is how the fact goes stale, and nothing
            # here could ever have noticed a tool that became dangerous.
            ann = entry.get("annotations")
            ann = ann if isinstance(ann, dict) else {}
            declared = ann.get("destructiveHint")
            if declared is None:
                declared = entry.get("hazardous")
            if declared is None:
                # An editor too old to send it. Fall back rather than call
                # everything safe, which is the failure that matters.
                destructive = name in _DESTRUCTIVE
                log.debug("no destructiveHint for %r; using the local set", name)
            else:
                destructive = bool(declared)
                if destructive != (name in _DESTRUCTIVE):
                    log.warning(
                        "destructive mismatch for %r: editor says %s, the set "
                        "in server.py says %s. Trusting the editor. Update "
                        "_DESTRUCTIVE or the tool's bHazardous so they agree.",
                        name, destructive, name in _DESTRUCTIVE)

            # idempotent_hint: true for reads by construction, and true for
            # mutating tools because dispatch deduplicates on operation_id.
            # Both hold, for different reasons. It used to be set to
            # mutating_flag, which said a READ was not idempotent.
            tools.append(types.Tool(
                name=name,
                title=_display_title(name),
                description=entry.get("description", ""),
                input_schema=schema,
                meta={_AREA_META_KEY: areas[name]},
                annotations=types.ToolAnnotations(
                    read_only_hint=not mutating_flag,
                    destructive_hint=destructive,
                    idempotent_hint=True,
                ),
            ))

        self._catalog = tools
        self._mutating = mutating
        self._areas = areas
        self._index.rebuild(tools, areas, mutating)

        log.info("catalog: %d tools (%d mutating) across %d areas",
                 len(tools), len(mutating), len(set(areas.values())))
        return tools

    # -- handlers ----------------------------------------------------------

    async def _handle_list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        self._remember_session(ctx)
        # The same list whether or not an editor is open, and whatever the
        # editor holds. Clients that read the list once (Claude Desktop and
        # Claude Code do) would otherwise be stuck with whatever they saw
        # first. The editor's own tools are reached through the gateway.
        return types.ListToolsResult(tools=[_BRIDGE_STATUS_TOOL, *_gateway.TOOLS])

    async def _handle_list_prompts(self, ctx: Any, params: Any) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=_playbooks.list_prompts())

    async def _handle_get_prompt(self, ctx: Any, params: Any) -> types.GetPromptResult:
        try:
            result = _playbooks.get_prompt(params.name, params.arguments, self._signature)
        except ValueError as exc:
            raise MCPError(-32602, str(exc)) from None
        self._enter_stage(_playbooks.resolve(str(params.name or "")))
        return result

    def _enter_stage(self, key: str | None) -> None:
        if key in _playbooks.ORDER:
            self._stage = key

    def _stage_refusal(self, tool: str) -> types.CallToolResult | None:
        """Refuse a change while the stage is one that changes nothing."""
        if self._stage not in ("design", "plan"):
            return None
        return _error(
            f"'{tool}' was not run: this is the {self._stage} stage, which changes "
            f"nothing. Finish the {self._stage} - write it to the user - and stop there. "
            f"The user starts building with the build command (/hullforge:build). "
            f"Only if the user has since told you plainly to make changes now, load the "
            f"build playbook with hf_search_tools {{\"playbook\": \"build\"}}, which lifts "
            f"this. Reading and hf_write with preview=true still work. Nothing was changed."
        )

    def _playbook(self, args: dict[str, Any]) -> types.CallToolResult:
        """hf_search_tools {"playbook": ...}: the playbook for clients without prompts."""
        key = _playbooks.resolve(str(args.get("playbook") or ""))
        if key is None:
            return _error(f"No playbook named '{args.get('playbook')}'. Playbooks: "
                          f"{', '.join(_playbooks.ORDER)}.")
        text = _playbooks.render(key, args.get("query") or None, self._signature)
        self._enter_stage(key)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content={"playbook": key, "next": _playbooks.next_stage(key)},
        )

    def _signature(self, tool: str) -> str | None:
        """One line a playbook can show instead of a search and a schema fetch:
        hf_add_state(asset, name, parent?, type?) -> hf_write."""
        entry = self._index.entries.get(tool)
        if entry is None:
            return None
        props = (entry.schema or {}).get("properties") or {}
        required = list((entry.schema or {}).get("required") or [])
        def show(arg: str) -> str:
            spec = props.get(arg) or {}
            kind = spec.get("type")
            if isinstance(kind, list):
                kind = next((k for k in kind if k != "null"), "any")
            short = {"string": "str", "integer": "int", "number": "num", "boolean": "bool",
                     "array": "list", "object": "obj"}.get(str(kind), "any")
            if spec.get("enum") and len(spec["enum"]) <= 5:
                short = "|".join(str(e) for e in spec["enum"])
            return arg + ("" if arg in required else "?") + ":" + short
        names = required + [a for a in props if a not in required and a not in ("force",)]
        shown = [show(a) for a in names[:12]]
        if len(names) > 12:
            shown.append("...")
        # THE CALLER COMES FIRST, written as the call is made. "name(args) ->
        # hf_write" read as a tool of its own: a demo agent called
        # mcp__hullforge__hf_add_blueprint_variable and was refused.
        return f"{_gateway.caller_for(entry.kind)} {entry.name} {{{', '.join(shown)}}}"

    async def _batch(self, args: dict[str, Any]) -> types.CallToolResult:
        """hf_batch: several calls in one round-trip.

        Measured on a demo take: 71 round-trips for one feature, each one
        re-reading the whole conversation, and most of them a step that only
        waited on the one before (create, then add, then set, then compile,
        then save). Here they run back to back with no model turn between.
        Every step goes through the same path as hf_read/hf_write, so the
        schema check, the stage lock and the failure notes all still apply."""
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            return _error("'steps' must be a non-empty list of {tool, args, as}. Nothing was changed.")
        verbose = args.get("verbose") is True
        named: dict[str, Any] = {}
        report: list[dict[str, Any]] = []
        touched: list[str] = []
        failed: dict[str, Any] | None = None

        label = args.get("checkpoint")
        if isinstance(label, str) and label.strip():
            cp = await self._gateway_invoke(_gateway.READ if self._kind("hf_set_checkpoint") == _gateway.READ_KIND
                                            else _gateway.WRITE,
                                            {"tool": "hf_set_checkpoint", "args": {"label": label.strip()}})
            if cp.is_error:
                return _error(f"The checkpoint could not be set, so no step was run: {_result_text(cp)}")

        for i, step in enumerate(steps):
            if not isinstance(step, dict) or not step.get("tool"):
                failed = {"step": i, "error": "each step needs 'tool'."}
                break
            tool = str(step["tool"])
            kind = self._kind(tool)
            if kind == _gateway.DESTROY_KIND:
                failed = {"step": i, "tool": tool,
                          "error": f"'{tool}' destroys work, so it is not run in a batch: call it "
                                   f"with hf_destroy on its own."}
                break
            try:
                inner = _resolve_refs(step.get("args") or {}, named)
            except _BadRef as exc:
                failed = {"step": i, "tool": tool, "error": f"bad reference: {exc}. Nothing in this step was run."}
                break
            caller = _gateway.READ if kind == _gateway.READ_KIND else _gateway.WRITE
            result = await self._gateway_invoke(caller, {"tool": tool, "args": inner, "verbose": verbose})
            if result.is_error:
                failed = {"step": i, "tool": tool, "error": _result_text(result)}
                break
            body = result.structured_content
            if body is None:
                try:
                    body = json.loads(_result_text(result))
                except ValueError:
                    body = _result_text(result)
            if step.get("as"):
                named[str(step["as"])] = body
            report.append({"step": i, "tool": tool, "result": body})
            if tool == "hf_create_blueprint":
                bp = (body.get("blueprint") if isinstance(body, dict) else None) or inner.get("destination")
            else:
                bp = inner.get("blueprint") if isinstance(inner, dict) else None
            if isinstance(bp, str) and bp.startswith("/"):
                bp = bp.split(".", 1)[0]
                if bp not in touched and tool != "hf_compile_blueprint":
                    touched.append(bp)

        out: dict[str, Any] = {"ran": len(report), "of": len(steps), "steps": report}
        if failed is not None:
            out["failed"] = failed
            out["not_run"] = len(steps) - len(report) - 1
            out["note"] = ("Steps before the failure stayed applied. Fix the failing step and send "
                           "it and the rest again" + (", or roll back to the checkpoint." if label else "."))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_compact(out))],
                structured_content=out, is_error=True)

        if args.get("compile", True) and touched and "hf_compile_blueprint" in self._index.entries:
            compiled = {}
            for bp in touched:
                r = await self._gateway_invoke(_gateway.WRITE, {"tool": "hf_compile_blueprint", "args": {"blueprint": bp}})
                b = r.structured_content or {}
                errors = b.get("errors") if isinstance(b, dict) else None
                compiled[bp] = ("failed: " + _result_text(r)[:400]) if r.is_error else (
                    "ok" if not errors else {"errors": errors, "messages": b.get("messages")})
            out["compiled"] = compiled
        if args.get("save") is True and "hf_save_all" in self._index.entries:
            r = await self._gateway_invoke(_gateway.WRITE, {"tool": "hf_save_all", "args": {}})
            out["saved"] = "ok" if not r.is_error else _result_text(r)[:400]
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=_compact(out))],
            structured_content=out)

    def _kind(self, tool: str) -> str | None:
        entry = self._index.entries.get(tool)
        return entry.kind if entry else None

    def _remember_session(self, ctx: Any) -> None:
        session = getattr(ctx, "session", None)
        if session is not None:
            self._mcp_session = session

    async def _handle_call_tool(self, ctx: Any, params: Any) -> types.CallToolResult:
        self._remember_session(ctx)

        name = params.name
        args = params.arguments or {}

        if name == _BRIDGE_STATUS_TOOL.name:
            # CONNECT FIRST, THEN REPORT. Answering "found but not yet
            # connected" for a live editor is true and useless: an agent that
            # checks status before anything else (it is preloaded, so it
            # does) read that as broken and stopped. A failed connect is
            # reported by the status below as it always was.
            if self._client is None or not self._client.connected:
                try:
                    if discover_all(self._project_dir):
                        await self._refresh_catalog()
                except (SessionError, BridgeError) as exc:
                    log.info("bridge status: connect attempt failed: %s", exc)
            return self._bridge_status()

        if name == _gateway.LOOKUP:
            return await self._api_lookup(args)

        if name == _gateway.BATCH:
            if not len(self._index) or self._client is None or not self._client.connected:
                try:
                    await self._refresh_catalog()
                except (SessionError, BridgeError) as exc:
                    return _error(str(exc))
            return await self._batch(args)

        if name == _gateway.SEARCH and args.get("playbook"):
            return self._playbook(args)

        if name in _gateway.NAMES:
            return await self._gateway_call(name, args)

        # Not listed, so not callable. The remedy is named rather than the call
        # quietly routed, so that a client's permission rules for hf_read,
        # hf_write and hf_destroy cannot be stepped around by calling the inner
        # tool by name.
        return _error(
            f"'{name}' is not called directly. Find it with hf_search_tools, check "
            f"its arguments with hf_tool_schema, then call it through hf_read, "
            f"hf_write or hf_destroy. Nothing was changed."
        )

    async def _dispatch(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        """Send one call to the editor, with the transport retry and the
        outcome-unknown handling."""
        is_mutating = name in self._mutating

        # One operation_id for the whole logical operation, reused across our
        # own retry. Generating a fresh id per attempt would dedupe nothing:
        # the id only helps when it is STABLE across retries of the same
        # operation. That is also why the retry lives here rather than being
        # left to the model - the model would produce a new id and apply the
        # mutation twice.
        op_id = BridgeClient.new_operation_id() if is_mutating else None

        attempts = 2  # original plus one transport retry
        last_error: BridgeError | None = None

        for attempt in range(attempts):
            try:
                client = await self._ensure_connected()
                result = await client.call(name, args, operation_id=op_id)
                deduplicated = client.last_call_deduplicated
            except ToolOutcomeUnknown as exc:
                return self._with_loss(
                    f"OUTCOME UNKNOWN for '{name}': {exc}\n\n"
                    f"The editor stopped waiting. This is NOT a reported failure - "
                    f"the operation may have taken effect. Check current state before "
                    f"retrying; retrying blind risks applying it twice."
                )
            except ToolFailed as exc:
                return _error(f"'{name}' failed: {exc}")
            except SessionError as exc:
                return self._with_loss(str(exc))
            except BridgeError as exc:
                # Transport died. Drop the client so the next attempt
                # re-discovers rather than reusing a half-dead socket.
                self._client = None
                last_error = exc
                if attempt + 1 < attempts:
                    log.warning("transport error on %r, retrying once: %s", name, exc)
                    continue
                return self._with_loss(
                    f"Bridge error calling '{name}': {exc}"
                    + (
                        ""
                        if is_mutating
                        else "\n\nThis was a read-only call, so nothing was changed."
                    )
                )

            # Keep the image bytes OUT of structured_content. They are already in
            # the image block, duplicating them doubles the payload, and some
            # clients drop content[] when structured_content is present - which
            # would lose the picture and leave a megabyte of base64 behind.
            #
            # AND THE STRUCTURED COPY IS THE LEAN ONE. Claude Code shows the
            # model structured_content, not the text, when both are present
            # (checked in a session transcript), so trimming only the text
            # trimmed nothing the agent read.
            lean = self._lean(name, result, terse=is_mutating and not self._verbose)
            structured = dict(lean) if isinstance(lean, dict) else {"result": lean}
            if deduplicated:
                structured["deduplicated"] = True

            return types.CallToolResult(
                content=self._to_content(name, result, deduplicated, lean),
                structured_content=structured,
            )

        # Unreachable in practice; the loop returns on every path.
        return _error(f"Bridge error calling '{name}': {last_error}")

    # -- engine API lookup --------------------------------------------------

    async def _api_lookup(self, args: dict[str, Any]) -> types.CallToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return _error("'query' is required: describe what you want to know, or give a class or function name.")
        limit = max(1, min(int(args.get("limit") or 6), 20))
        kind = args.get("kind") or None
        editor_only = bool(args.get("editor_only"))
        self._api.ensure_started()
        if self._api.index is None:
            # First use: reading the engine headers takes about twenty seconds.
            await asyncio.to_thread(self._api.wait, 45)
        index = self._api.index
        if index is None:
            why = self._api.error or "still reading the engine headers; try again in a few seconds"
            if not self._api.error and not _api_index.find_engines():
                why = "no Unreal Engine install was found in the Epic launcher's list"
            return _error(f"The API index is not ready: {why}.")
        hits = await asyncio.to_thread(index.search, query, limit=limit, kind=kind, editor_only=editor_only)
        status = index.status
        method = "hybrid (keywords + embeddings)" if status.embeddings == "ready" else (
            f"keywords only; embeddings {status.embeddings}")
        rows = []
        lines = [f"{len(hits)} results for '{query}' from UE {status.engine or '?'} "
                 f"({status.records} entries, {method})."]
        for _, r in hits:
            row = {"kind": r.kind, "name": r.name, "module": r.module, "path": r.path,
                   "signature": r.signature, "category": r.category, "editor": r.editor,
                   "text": r.text[:700]}
            rows.append(row)
            head = f"- [{r.kind}] {r.name}"
            if r.module:
                head += f"  ({r.module}{', editor' if r.editor else ''})"
            lines.append(head)
            if r.signature:
                lines.append(f"    {r.signature}")
            if r.text:
                lines.append(f"    {r.text[:400]}" + ("..." if len(r.text) > 400 else ""))
            if r.path:
                lines.append(f"    {r.path}")
        if not hits:
            lines.append("Nothing matched. Try other words or a class name.")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="\n".join(lines))],
            structured_content={"engine": status.engine, "method": method, "results": rows},
        )

    # -- gateway ------------------------------------------------------------

    async def _gateway_call(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        if not len(self._index) or self._client is None or not self._client.connected:
            try:
                await self._refresh_catalog()
            except (SessionError, BridgeError) as exc:
                return _error(str(exc))
        if name == _gateway.SEARCH:
            return self._gateway_search(args)
        if name == _gateway.SCHEMA:
            return self._gateway_schema(args)
        return await self._gateway_invoke(name, args)

    def _suggest(self, text: str, limit: int = 5) -> list[str]:
        return [e.name for _, e in self._index.search(text.replace("_", " "), limit=limit)]

    def _gateway_search(self, args: dict[str, Any]) -> types.CallToolResult:
        # SEVERAL QUERIES IN ONE CALL. A rehearsal agent made 17 searches to
        # find a dozen well-known tools, one need at a time, and a single query
        # naming four needs ranked an unrelated tool first. Each query here is
        # ranked on its own and the answers come back together.
        queries = args.get("queries")
        if isinstance(queries, list) and queries:
            parts, rows = [], []
            for q in [str(x) for x in queries[:10] if str(x).strip()]:
                one = self._gateway_search({**{k: v for k, v in args.items() if k != "queries"},
                                            "query": q, "_default_limit": 4})
                parts.append(f"## {q}\n{_result_text(one)}")
                for r in (one.structured_content or {}).get("results", []):
                    rows.append({**r, "query": q})
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="\n\n".join(parts))],
                structured_content={"results": rows},
            )
        query = str(args.get("query") or "").strip()
        # FIVE BY DEFAULT, FOUR EACH WHEN SEVERAL QUERIES ARE ASKED AT ONCE.
        # Search results stay in context; the eighth hit for a need is almost
        # never the one called.
        limit = max(1, min(int(args.get("limit") or args.get("_default_limit") or 5), 25))
        kind = args.get("kind") or None
        area = None
        if args.get("area"):
            area = self._index.resolve_area(str(args["area"]))
            if area is None:
                return _error(
                    f"No area named '{args['area']}'. Areas: "
                    f"{', '.join(self._index.areas())}. Nothing was searched."
                )

        if not query and area is None:
            counts = self._index.areas()
            lines = [f"{len(counts)} areas, {len(self._index)} tools. Search with a query, "
                     f"or pass an area to list its tools."]
            lines += [f"  {a} ({n})" for a, n in counts.items()]
            lines += ["", "Playbooks for a whole task, in order (hf_search_tools with "
                      "'playbook'):", _playbooks.overview()]
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="\n".join(lines))],
                structured_content={"areas": counts},
            )

        if not query:
            entries = sorted((e for e in self._index.entries.values()
                              if e.area == area and (not kind or e.kind == kind)),
                             key=lambda e: e.name)
            hits = [(0.0, e) for e in entries]
            header = f"{len(entries)} tools in {area}."
        else:
            hits = self._index.search(query, area=area, kind=kind, limit=limit)
            header = (f"{len(hits)} matches for '{query}'"
                      + (f" in {area}" if area else "") + ". Next: hf_tool_schema with the "
                      "names you will call." if hits else
                      f"Nothing matched '{query}'. Try other words, or list an area "
                      f"(hf_search_tools with only 'area').")

        rows = [{"name": e.name,
                 "call_with": _gateway.caller_for(e.kind),
                 "call": _gateway.call_example(e),
                 "required": _gateway.required_args(e), "summary": e.summary} for _, e in hits]
        lines = [header]
        for r in rows:
            # The call line already carries the required arguments.
            lines.append(f"- {r['name']}: {r['summary']}\n  call: {r['call']}")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="\n".join(lines))],
            structured_content={"results": rows},
        )

    def _gateway_schema(self, args: dict[str, Any]) -> types.CallToolResult:
        names = args.get("names") or []
        if not isinstance(names, list) or not names:
            return _error("'names' must be a non-empty array of tool names from hf_search_tools.")
        out: list[dict[str, Any]] = []
        for raw in names[:10]:
            entry = self._index.entries.get(str(raw))
            if entry is None:
                if str(raw) in _TOP_LEVEL:
                    # One of the tools beside the catalog: call it as it is.
                    out.append({"name": raw, "call": f"Call {raw} directly; it is one of "
                                "HullForge's own tools, not one reached through hf_read/hf_write."})
                    continue
                out.append({"name": raw, "error": "no such tool",
                            "did_you_mean": self._suggest(str(raw))})
                continue
            out.append({
                "name": entry.name,
                "call_with": _gateway.caller_for(entry.kind),
                "call": _gateway.call_example(entry),
                "description": entry.description,
                "input_schema": _gateway.slim_schema(entry.schema),
            })
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=_compact(out))],
            structured_content={"tools": out},
        )

    async def _gateway_invoke(self, caller: str, args: dict[str, Any]) -> types.CallToolResult:
        """Run one inner tool, then make a failure as cheap to recover from as
        possible and log the outcome. Every tool in the catalog passes through
        here, so this is where a hint helps all of them at once."""
        result = await self._gateway_invoke_inner(caller, args)
        return self._after_call(caller, args, result)

    def _after_call(self, caller: str, args: dict[str, Any],
                    result: types.CallToolResult) -> types.CallToolResult:
        tool = str(args.get("tool") or "")
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        ok = not result.is_error
        message = _result_text(result)
        repeat = 0
        if not ok and not _is_transport_failure(message, result):
            key = (tool, json.dumps(inner, sort_keys=True, default=str))
            first = message.strip().splitlines()[0] if message.strip() else ""
            prev = self._failures.get(key)
            repeat = prev[0] + 1 if prev and prev[1] == first else 1
            self._failures[key] = (repeat, first)
            notes = self._failure_notes(tool, inner, message, repeat)
            if notes:
                result = _with_text(result, f"{message}\n\n{notes}")
        self._friction.record(caller=caller, tool=tool, ok=ok, error=message,
                              repeat=repeat, arg_names=list(inner))
        return result

    def _failure_notes(self, tool: str, inner: dict[str, Any], message: str,
                       repeat: int) -> str:
        """What to change before the next attempt.

        THE SAME CALL FAILING AGAIN is the costliest pattern there is: a
        rehearsal agent sent an identical refused call twice in three of its
        eight snags. So a repeat says so, plainly.

        AND THE ARGUMENTS' OWN DOCUMENTATION, from the tool's schema, for the
        arguments that were sent and any the error names. Most refusals are
        about a value's form, and the form is written down in the schema the
        agent did not fetch. Only those arguments, not the whole schema: this
        rides on every failure and has to stay small.
        """
        lines: list[str] = []
        if repeat >= 2:
            lines.append(
                f"THIS EXACT CALL HAS NOW FAILED {repeat} TIMES with the same error. "
                f"Repeating it will not change the answer: change the arguments as the "
                f"error says, or find another tool with hf_search_tools."
            )
        entry = self._index.entries.get(tool)
        if entry is not None and "Schema for" not in message:
            props = (entry.schema or {}).get("properties") or {}
            names = [n for n in inner if n in props]
            names += [n for n in props if n not in names
                      and re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", message)]
            notes = []
            for n in names[:6]:
                about = str((props.get(n) or {}).get("description") or "").strip()
                if about:
                    notes.append(f"- {n}: {about[:300]}")
            if notes:
                lines.append(f"From {tool}'s schema:\n" + "\n".join(notes))
        return "\n\n".join(lines)

    async def _gateway_invoke_inner(self, caller: str, args: dict[str, Any]) -> types.CallToolResult:
        self._verbose = args.get("verbose") is True
        tool = str(args.get("tool") or "")
        inner = args.get("args") or {}
        if not isinstance(inner, dict):
            return _error("'args' must be an object of the tool's arguments. Nothing was changed.")
        entry = self._index.entries.get(tool)
        if entry is None:
            hint = self._suggest(tool)
            return _error(
                f"No tool named '{tool}'."
                + (f" Closest: {', '.join(hint)}." if hint else "")
                + " Find tools with hf_search_tools. Nothing was changed."
            )

        wanted = _gateway.kind_of_caller(caller)
        if entry.kind != wanted:
            # Refused, not rerouted. The caller is what a client's permission
            # rules see; a destroy that arrived as hf_write would get past a
            # rule that asks before every hf_destroy.
            return _error(
                f"'{tool}' is a {entry.kind} tool, so it is called with "
                f"{_gateway.caller_for(entry.kind)}, not {caller}. Nothing was changed."
            )

        preview = args.get("preview") is True and caller != _gateway.READ

        if caller != _gateway.READ and not preview:
            refused = self._stage_refusal(tool)
            if refused is not None:
                return refused

        if caller == _gateway.DESTROY:
            if args.get("confirm") is not True and not preview:
                return _error(
                    f"hf_destroy refused '{tool}': confirm must be true. Set it only "
                    f"for a deletion the user asked for. Nothing was changed."
                )
            props = entry.schema.get("properties") or {}
            if "confirm" in props and "confirm" not in inner:
                inner = {**inner, "confirm": True}

        inner = _gateway.coerce_args(entry, inner)
        problems = _gateway.validate(entry, inner)
        if problems:
            # The schema comes back with the refusal, so the next attempt can
            # be right without another round-trip to hf_tool_schema.
            return _error(
                f"Arguments for '{tool}' don't match its schema, so nothing was sent "
                f"to the editor:\n- " + "\n- ".join(problems)
                + f"\n\nSchema for {tool}:\n{_compact(_gateway.slim_schema(entry.schema))}"
            )

        if preview:
            # Run under an undo transaction in the editor and rolled back there;
            # nothing changes, so a destroy preview needs no confirm.
            return await self._dispatch("hf_preview_write", {"tool": tool, "args": inner})
        if tool in _FOCUS_FOR_CAPTURE and not inner.get("window"):
            return await self._capture_in_front(tool, inner)
        if tool == "hf_pie_run_playtest":
            return await self._run_playtest_and_wait(inner)
        return await self._dispatch(tool, inner)

    async def _run_playtest_and_wait(self, inner: dict[str, Any]) -> types.CallToolResult:
        """Start a playtest and, by default, wait for its finished report.

        A run plays out over later editor frames, so the editor cannot answer
        with the result; the rehearsal agent polled hf_pie_get_playtest two or
        three times per run for playtests that took under three seconds. The
        wait happens here, outside the editor's game thread, so it blocks
        nothing. wait_seconds=0 keeps the old start-and-poll behaviour.
        """
        try:
            wait = float(inner.get("wait_seconds", 30))
        except (TypeError, ValueError):
            wait = 30.0
        wait = max(0.0, min(wait, 120.0))
        started = await self._dispatch("hf_pie_run_playtest", inner)
        run_id = (started.structured_content or {}).get("run_id")
        if started.is_error or wait <= 0 or run_id is None:
            return started
        deadline = time.monotonic() + wait
        report = started
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            report = await self._dispatch("hf_pie_get_playtest", {"run_id": run_id})
            status = (report.structured_content or {}).get("status")
            if report.is_error or status not in ("starting_pie", "running", "queued", "pending"):
                return report
        note = (f"\n\nStill running after {wait:.0f} s. Poll hf_pie_get_playtest with "
                f"run_id {run_id} for the rest.")
        return _with_text(report, _result_text(report) + note)

    async def _capture_in_front(self, tool: str, args: dict[str, Any]) -> types.CallToolResult:
        """A window capture, with the editor brought to the front for it.

        A window capture shows what the editor last drew, and a background
        editor draws rarely or not at all, so its 3D view is stale. Bringing
        it forward for the moment of the capture, from this process, which
        Windows allows - see focus.py - means nobody has to click the editor
        first. Focus goes back to whatever had it straight after.
        """
        try:
            await self._ensure_connected()
        except (SessionError, BridgeError):
            return await self._dispatch(tool, args)

        state, hwnd, previous = await asyncio.to_thread(_focus.bring_forward, self._editor_pid)
        try:
            result = await self._dispatch(tool, args)
        finally:
            await asyncio.to_thread(_focus.restore, state, hwnd, previous)

        focus_report = {
            "editor_was_in_front": state.was_foreground,
            "brought_forward": state.brought_forward,
            "focus_returned": state.restored_previous,
        }
        if state.note:
            focus_report["note"] = state.note
        if isinstance(result.structured_content, dict):
            result.structured_content["focus"] = focus_report
        if state.window_found and not state.was_foreground and not state.brought_forward:
            result.content.append(types.TextContent(
                type="text",
                text=("The editor could not be brought to the front, so its 3D view in "
                      "this picture may be an old frame. Ask the user to click the Unreal "
                      "window, or during play use hf_pie_capture, which renders offscreen "
                      "and needs no focus."),
            ))
        return result

    def _lean(self, name: str, result: Any, terse: bool = False) -> Any:
        """What the agent reads: repeats shortened, then fitted to the budget.

        TERSE for a successful write unless 'verbose' was asked for: the
        caveats every write carries (what was not checked, the packages it
        dirtied, undo and deprecation flags) averaged 600-2,000 characters a
        call and were almost never acted on. Failures never get here - they
        are errors, reported in full."""
        if isinstance(result, dict):
            result = {k: v for k, v in result.items() if k != "image_base64"}
            if terse:
                result = {k: v for k, v in result.items()
                          if k not in _TERSE_DROP and not (v is False and k in _FALSE_IS_NEWS_ONLY_IF_TRUE)}
        return json.loads(_fit_json(self._shrink_repeats(name, result)))

    def _to_content(self, name: str, result: dict[str, Any], deduplicated: bool = False,
                    lean: Any = None) -> list[Any]:
        """Turn a tool result into MCP content blocks.

        A tool that returns image_base64 gets a real ImageContent block. This is
        the difference between the model seeing a picture and the model being
        handed a megabyte of base64 as characters to read - which is useless and
        would swallow the context window.

        The base64 is stripped from the accompanying text for the same reason:
        the bytes belong in the image block and nowhere else.
        """
        note = (
            "\n\nNote: this operation had already completed; the editor replayed "
            "its original result rather than running it again."
            if deduplicated
            else ""
        )

        image_b64 = result.get("image_base64")
        if isinstance(image_b64, str) and image_b64:
            meta = lean if lean is not None else self._lean(name, result)
            blocks: list[Any] = [
                types.ImageContent(
                    type="image",
                    data=image_b64,
                    mime_type=str(result.get("mime_type", "image/png")),
                )
            ]
            if meta:
                blocks.append(types.TextContent(
                    type="text", text=_compact(meta) + note))
            return blocks

        if lean is None:
            lean = self._lean(name, result)
        return [types.TextContent(type="text", text=_compact(lean) + note)]

    def _shrink_repeats(self, tool: str, result: Any) -> Any:
        """Send each long, repeated note once per session.

        Measured on a demo session: every write result re-sent the same
        paragraphs (what was not checked, what a dirty Blueprint means, the
        'force' advice), about 34,000 characters of repeats, and every one of
        them stays in the agent's context for the rest of the session. The
        first copy is kept; later ones point back to it. Short values,
        errors and anything the agent could act on differently are never
        touched, because only strings over 160 characters seen before, word
        for word, are replaced."""
        if isinstance(result, dict):
            out: dict[str, Any] = {}
            for k, v in result.items():
                if isinstance(v, str) and len(v) > 160 and k not in _NEVER_SHRINK:
                    first = self._said.get(v)
                    if first is None:
                        self._said[v] = f"{k} of {tool}"
                        out[k] = v
                    else:
                        out[k] = f"(same as the {first} earlier)"
                else:
                    out[k] = self._shrink_repeats(tool, v)
            return out
        if isinstance(result, list):
            return [self._shrink_repeats(tool, x) for x in result]
        return result

    def _bridge_status(self) -> types.CallToolResult:
        """Answerable with no editor running - that is the entire point."""
        sessions = discover_all(self._project_dir)
        connected = self._client is not None and self._client.connected

        payload: dict[str, Any] = {
            "connected": connected,
            "editors_found": len(sessions),
            "registry": str(session_registry_dir()),
            "editors": [
                {
                    "project": s.project_name,
                    "project_dir": s.project_dir,
                    "port": s.port,
                    "pid": s.pid,
                    "source_edits": "on" if s.source_edits else "off",
                    "active": (s.port, s.token) == self._session_key,
                }
                for s in sessions
            ],
            "tool_count": len(self._catalog),
            # Where this session's calls went wrong; see friction.py.
            "friction": self._friction.summary(),
        }
        loss = self._editor_loss() if not connected else None
        if loss is not None:
            payload["last_editor_lost"] = loss

        if loss is not None and not any(
                s.project_dir == (self._last_editor or {}).get("project_dir") for s in sessions):
            message = f"NOT CONNECTED - {loss['summary']}"
        elif not sessions:
            message = (
                "NOT CONNECTED - no running Unreal editor was found.\n\n"
                "Open your project in Unreal Engine with the HullForge plugin "
                "enabled. The remaining tools will appear on their own within a "
                "second or two; you do NOT need to restart this client.\n\n"
                f"Editors advertise themselves in: {session_registry_dir()}"
            )
        elif not connected:
            message = (
                f"{len(sessions)} editor(s) found but not yet connected. The next "
                f"tool call will connect automatically."
            )
        else:
            active = next((s for s in sessions
                           if (s.port, s.token) == self._session_key), None)
            where = active.project_name if active else "an editor"
            edits = "on" if active is not None and active.source_edits else "off"
            message = (
                f"Connected to {where} with {len(self._catalog)} tools available. "
                f"C++ source edits are {edits}"
                + ("." if edits == "on" else
                   " (the user turns them on in Editor Preferences > Plugins > "
                   "HullForge Source Edits).")
            )

        return types.CallToolResult(
            content=[types.TextContent(
                type="text",
                text=f"{message}\n\n{json.dumps(payload, indent=2)}",
            )],
            structured_content=payload,
        )

    async def _watch_for_editors(self) -> None:
        """Notice an editor appearing or going away.

        Drops a dead connection, so the next call rediscovers instead of using
        a socket to an editor that has closed, and forgets the catalog, so the
        next gateway call fetches the one the current editor holds.
        """
        import anyio

        previous: set[tuple[int, str]] = set()
        while True:
            await anyio.sleep(_WATCH_INTERVAL_SECONDS)

            try:
                current = {(s.port, s.token) for s in discover_all(self._project_dir)}
            except Exception:  # noqa: BLE001 - a watcher must never die
                continue

            if current == previous:
                continue
            previous = current

            # The editor we were talking to is gone; drop the socket so the
            # next call rediscovers instead of using a dead one.
            if self._session_key is not None and self._session_key not in current:
                log.info("editor went away; dropping connection")
                self._client = None
                self._session_key = None
                self._editor_loss()

            # The tool list never changes, but the catalog behind it may: a
            # different editor can hold different tools. Forget it, and the next
            # gateway call fetches the editor's catalog afresh.
            self._index = _gateway.Index()
            log.info("editor set changed (%d now); catalog will be refetched",
                     len(current))

    async def run_stdio(self) -> None:
        import anyio
        from mcp.server.stdio import stdio_server

        # The tool list is fixed, so there is never a change to announce.
        init_options = self.server.create_initialization_options(
            NotificationOptions(tools_changed=False)
        )

        async with stdio_server() as (read, write):
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._watch_for_editors)
                try:
                    await self.server.run(read, write, init_options)
                finally:
                    tg.cancel_scope.cancel()


# The tools listed beside the catalog, which are called directly.
_TOP_LEVEL = {"hf_bridge_status", *_gateway.NAMES, _gateway.LOOKUP, _gateway.BATCH}


# Claude Code refuses a tool result over about 25,000 tokens and parks it in a
# file the agent then has to script its way through; a 200-row asset search
# did that on camera. So every result is fitted to a budget here, once, for
# all 3,000 tools: compact JSON first, then the longest lists trimmed, saying
# exactly what was left out and how to get the rest.
RESULT_CHAR_BUDGET = 12000


def _compact(value: Any) -> str:
    # NO INDENTATION. Pretty-printed JSON spent about a third of every result
    # on whitespace, and every result stays in the agent's context for the
    # rest of the session, re-read on every turn.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_]+)*)")


class _BadRef(KeyError):
    def __str__(self) -> str:
        return str(self.args[0])


def _resolve_refs(value: Any, named: dict[str, Any]) -> Any:
    """Replace $name.field references with earlier step results (hf_batch).

    A string that is ONLY a reference becomes the referenced value with its
    type; a reference inside a longer string is spliced in as text. A name
    that is not a step raises KeyError; anything else that merely starts with
    '$' and names no step is left as written."""
    def lookup(name: str, path: str) -> Any:
        if name not in named:
            raise _BadRef(f"${name} names no earlier step; names so far: {', '.join(named) or 'none'}")
        v = named[name]
        for part in [p for p in path.split(".") if p]:
            if isinstance(v, dict) and part in v:
                v = v[part]
            elif isinstance(v, list) and part.isdigit() and int(part) < len(v):
                v = v[int(part)]
            else:
                # SAY WHAT IS THERE. "names no earlier step result" for a
                # step that exists but has no such field sent a demo agent
                # looking for the wrong mistake.
                have = ", ".join(v) if isinstance(v, dict) else type(v).__name__
                raise _BadRef(f"${name}{path}: step '{name}' has no '{part}'; its result has: {have}")
        return v
    if isinstance(value, dict):
        return {k: _resolve_refs(v, named) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, named) for v in value]
    if isinstance(value, str) and "$" in value:
        whole = _REF.fullmatch(value)
        if whole:
            return lookup(whole.group(1), whole.group(2))
        return _REF.sub(lambda m: str(lookup(m.group(1), m.group(2))), value)
    return value


# Dropped from a successful write's result unless 'verbose' (see _lean).
_TERSE_DROP = {"not_checked", "freshness", "touched", "verification_note", "target_id_note",
               "access_advice", "status_says_compiled", "transactional", "package_dirty",
               "undoable", "instances_note", "property_type", "object_path", "generated_class"}
# Flags that only say something when true.
_FALSE_IS_NEWS_ONLY_IF_TRUE = {"deprecated", "changed", "deduplicated"}

# Keys whose text is the answer itself, never boilerplate.
_NEVER_SHRINK = {"error", "message", "text", "value", "value_text", "before", "after",
                 "source", "content", "code", "log", "lines", "path", "name"}


def _fit_json(result: Any, budget: int = RESULT_CHAR_BUDGET) -> str:
    text = _compact(result)
    if len(text) <= budget or not isinstance(result, dict):
        return text
    fitted = dict(result)
    trimmed: dict[str, str] = {}
    lists = sorted((k for k, v in fitted.items() if isinstance(v, list) and len(v) > 1),
                   key=lambda k: -len(json.dumps(fitted[k], separators=(",", ":"))))
    for key in lists:
        full = fitted[key]
        lo, hi = 1, len(full)
        while lo < hi:  # the most rows that still fit
            mid = (lo + hi + 1) // 2
            fitted[key] = full[:mid]
            if len(json.dumps(fitted, separators=(",", ":"))) <= budget - 400:
                lo = mid
            else:
                hi = mid - 1
        fitted[key] = full[:lo]
        if lo < len(full):
            trimmed[key] = f"{lo} of {len(full)} shown"
        if len(json.dumps(fitted, separators=(",", ":"))) <= budget - 400:
            break
    if trimmed:
        fitted["_trimmed"] = {
            **trimmed,
            "why": "The full result was too large to read in one answer.",
            "next": "Narrow the call (a filter, a path, a smaller limit) or page with offset to see the rest.",
        }
    return json.dumps(fitted, separators=(",", ":"))


def _result_text(result: types.CallToolResult) -> str:
    return "\n".join(c.text for c in (result.content or []) if isinstance(c, types.TextContent))


def _with_text(result: types.CallToolResult, text: str) -> types.CallToolResult:
    """The same result with its text replaced, structured content kept."""
    out = types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                               is_error=result.is_error)
    out.structured_content = result.structured_content
    return out


def _is_transport_failure(message: str, result: types.CallToolResult) -> bool:
    """Not the tool's answer but the bridge's: nothing about arguments to fix."""
    if result.structured_content and "editor_lost" in (result.structured_content or {}):
        return True
    return (message.startswith("OUTCOME UNKNOWN")
            or "No running Unreal editor" in message
            or "transport" in message.lower()[:200])


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        is_error=True,
    )


def main() -> None:
    import anyio

    logging.basicConfig(
        level=os.environ.get("HULLFORGE_LOG", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Optional now. Editors advertise themselves in a user-global registry, so
    # the common case needs no configuration at all. Setting it only adds that
    # project's own session file as an extra place to look.
    project_dir = os.environ.get("HULLFORGE_PROJECT_DIR") or None

    anyio.run(HullForgeServer(project_dir).run_stdio)


if __name__ == "__main__":
    main()
