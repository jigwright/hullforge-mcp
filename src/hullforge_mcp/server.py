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

import json
import logging
import os
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server

from .client import BridgeClient, BridgeError, ToolFailed, ToolOutcomeUnknown
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

# Always present, even with no editor running. Without this, "no editor" shows
# up as an empty tool list, which is silent and baffling - the user has no way
# to tell a broken install from an editor they forgot to open.
_BRIDGE_STATUS_TOOL = types.Tool(
    name="hf_bridge_status",
    title="Bridge Status",
    description=(
        "Whether HullForge is connected to a running Unreal editor, and what to "
        "do if not. This tool is always available even when no editor is open. "
        "When an editor starts, the rest of the tools appear automatically - "
        "there is no need to restart the client."
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
}


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
        self._mutating: set[str] = set()
        self._session_key: tuple[int, str] | None = None
        self._mcp_session: Any = None
        self.server: Server = Server(
            "hullforge",
            version="0.1.0",
            instructions=(
                "Drives a running Unreal Engine 5.8 editor. If no tools beyond "
                "hf_bridge_status are listed, no editor is open - start one and the "
                "rest appear automatically, without restarting this client. Tools "
                "that mutate the project are annotated as such. If a call reports "
                "that the outcome is UNKNOWN, the operation may still have taken "
                "effect - verify current state before retrying, or you risk "
                "applying it twice."
            ),
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
                "Open your project and the tools will appear automatically - no "
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
                annotations=types.ToolAnnotations(
                    read_only_hint=not mutating_flag,
                    destructive_hint=destructive,
                    idempotent_hint=mutating_flag,  # we supply an operation_id
                ),
            ))

        self._catalog = tools
        self._mutating = mutating
        log.info("catalog: %d tools (%d mutating)", len(tools), len(mutating))
        return tools

    # -- handlers ----------------------------------------------------------

    async def _handle_list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        # Capture the session so the watcher can push notifications later.
        # The client always calls tools/list right after initialize, so this is
        # set within milliseconds of connecting.
        self._remember_session(ctx)

        try:
            tools = await self._refresh_catalog()
        except (SessionError, BridgeError) as exc:
            # No editor. Return ONLY the status tool rather than an empty list -
            # an empty list looks identical to a broken install, whereas a
            # single tool that explains itself is actionable.
            log.info("no editor available: %s", exc)
            tools = []

        # Deliberately no ttl_ms: the catalog changes whenever HullForge.Reload
        # runs, and a cached stale catalog is worse than re-fetching.
        return types.ListToolsResult(tools=[_BRIDGE_STATUS_TOOL, *tools])

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
        """Notify the client when an editor appears or goes away.

        This is what removes the restart. MCP servers are launched by the
        client, so a sidecar that starts before the editor would otherwise
        report an empty tool list forever - the client has no reason to ask
        again. Declaring listChanged and pushing a notification lets the tools
        show up the moment the editor is ready.
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

            if self._mcp_session is None:
                continue

            try:
                await self._mcp_session.send_tool_list_changed()
                log.info("editor set changed (%d now); told the client to refresh",
                         len(current))
            except Exception as exc:  # noqa: BLE001
                log.debug("could not send tools/list_changed: %s", exc)

    async def run_stdio(self) -> None:
        import anyio
        from mcp.server.stdio import stdio_server

        # tools_changed is what makes the watcher meaningful. Without it the
        # client is told listChanged=false and ignores the notification.
        init_options = self.server.create_initialization_options(
            NotificationOptions(tools_changed=True)
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
