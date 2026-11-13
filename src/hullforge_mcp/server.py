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
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server

from .client import BridgeClient, BridgeError, ToolFailed, ToolOutcomeUnknown
from . import api_index as _api_index
from . import gateway as _gateway
from .session import (
    SessionError,
    SessionInfo,
    discover,
    discover_all,
    session_registry_dir,
)

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
    meta={_AREA_META_KEY: _ALWAYS_ON_AREA},
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
        self._mcp_session: Any = None
        # The gateway's view of the editor's catalog: searched, validated
        # against, never listed. See gateway.py.
        self._index = _gateway.Index()
        # The engine API lookup. Built from the installed engine on first use,
        # in a thread, so it never blocks the editor tools.
        self._api = _api_index.LazyApiIndex()
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

    # -- bridge ------------------------------------------------------------

    async def _ensure_connected(self) -> BridgeClient:
        if self._client is not None and self._client.connected:
            return self._client

        info = self._pick_session()                 # raises SessionError
        client = BridgeClient(info)
        await client.connect()                      # raises BridgeError
        self._client = client
        self._session_key = (info.port, info.token)
        log.info("connected to %s on port %d (v%s)",
                 info.project_name or "editor", info.port, client.server_version)
        return client

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
            destructive = name in _DESTRUCTIVE

            tools.append(types.Tool(
                name=name,
                title=_display_title(name),
                description=entry.get("description", ""),
                input_schema=schema,
                meta={_AREA_META_KEY: areas[name]},
                annotations=types.ToolAnnotations(
                    read_only_hint=not mutating_flag,
                    destructive_hint=destructive,
                    idempotent_hint=mutating_flag,  # we supply an operation_id
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

    def _remember_session(self, ctx: Any) -> None:
        session = getattr(ctx, "session", None)
        if session is not None:
            self._mcp_session = session

    async def _handle_call_tool(self, ctx: Any, params: Any) -> types.CallToolResult:
        self._remember_session(ctx)

        name = params.name
        args = params.arguments or {}

        if name == _BRIDGE_STATUS_TOOL.name:
            return self._bridge_status()

        if name == _gateway.LOOKUP:
            return await self._api_lookup(args)

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
                return _error(
                    f"OUTCOME UNKNOWN for '{name}': {exc}\n\n"
                    f"The editor stopped waiting. This is NOT a reported failure - "
                    f"the operation may have taken effect. Check current state before "
                    f"retrying; retrying blind risks applying it twice."
                )
            except ToolFailed as exc:
                return _error(f"'{name}' failed: {exc}")
            except SessionError as exc:
                return _error(str(exc))
            except BridgeError as exc:
                # Transport died. Drop the client so the next attempt
                # re-discovers rather than reusing a half-dead socket.
                self._client = None
                last_error = exc
                if attempt + 1 < attempts:
                    log.warning("transport error on %r, retrying once: %s", name, exc)
                    continue
                return _error(
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
            structured = {k: v for k, v in result.items() if k != "image_base64"}
            if deduplicated:
                structured["deduplicated"] = True

            return types.CallToolResult(
                content=self._to_content(name, result, deduplicated),
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
        query = str(args.get("query") or "").strip()
        limit = max(1, min(int(args.get("limit") or 8), 25))
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

        rows = [{"name": e.name, "area": e.area, "kind": e.kind,
                 "call_with": _gateway.caller_for(e.kind),
                 "required": _gateway.required_args(e), "summary": e.summary} for _, e in hits]
        lines = [header]
        for r in rows:
            lines.append(f"- {r['name']}  [{r['area']} | {r['kind']} -> {r['call_with']}]\n"
                         f"    {r['summary']}")
            for arg, about in r["required"].items():
                lines.append(f"    required {arg} ({about})")
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
                out.append({"name": raw, "error": "no such tool",
                            "did_you_mean": self._suggest(str(raw))})
                continue
            out.append({
                "name": entry.name, "area": entry.area, "kind": entry.kind,
                "call_with": _gateway.caller_for(entry.kind),
                "description": entry.description,
                "input_schema": entry.schema,
            })
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(out, indent=2))],
            structured_content={"tools": out},
        )

    async def _gateway_invoke(self, caller: str, args: dict[str, Any]) -> types.CallToolResult:
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

        if caller == _gateway.DESTROY:
            if args.get("confirm") is not True:
                return _error(
                    f"hf_destroy refused '{tool}': confirm must be true. Set it only "
                    f"for a deletion the user asked for. Nothing was changed."
                )
            props = entry.schema.get("properties") or {}
            if "confirm" in props and "confirm" not in inner:
                inner = {**inner, "confirm": True}

        problems = _gateway.validate(entry, inner)
        if problems:
            # The schema comes back with the refusal, so the next attempt can
            # be right without another round-trip to hf_tool_schema.
            return _error(
                f"Arguments for '{tool}' don't match its schema, so nothing was sent "
                f"to the editor:\n- " + "\n- ".join(problems)
                + f"\n\nSchema for {tool}:\n{json.dumps(entry.schema, indent=2)}"
            )

        return await self._dispatch(tool, inner)

    @staticmethod
    def _to_content(name: str, result: dict[str, Any], deduplicated: bool = False) -> list[Any]:
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
            meta = {k: v for k, v in result.items() if k != "image_base64"}
            blocks: list[Any] = [
                types.ImageContent(
                    type="image",
                    data=image_b64,
                    mime_type=str(result.get("mime_type", "image/png")),
                )
            ]
            if meta:
                blocks.append(types.TextContent(
                    type="text", text=json.dumps(meta, indent=2) + note))
            return blocks

        return [types.TextContent(type="text", text=json.dumps(result, indent=2) + note)]

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
                    "active": (s.port, s.token) == self._session_key,
                }
                for s in sessions
            ],
            "tool_count": len(self._catalog),
        }

        if not sessions:
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
            message = (
                f"Connected to {where} with {len(self._catalog)} tools available."
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
