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

# --- catalog mode ------------------------------------------------------------
#
# Progressive tool disclosure. With well over a hundred tools, listing all of
# them costs the model tens of thousands of tokens of context before it has done
# anything, and most of a session touches two or three areas. Catalog mode lists
# a small always-on core plus whatever areas have been switched on, and pushes
# tools/list_changed when the set changes - the same mechanism that already makes
# tools appear when an editor opens, so no client needs to do anything new.
#
# OFF BY DEFAULT, and it stays that way. Everything else in this product is
# "what you see is what dispatches", and a mode that silently hides two thirds
# of the tools would break that for someone who never asked for it. It is opt-in
# by environment variable, and hf_list_tool_areas says it exists and what it
# would save whether or not it is on.
_CATALOG_MODE_ENV = "HULLFORGE_CATALOG_MODE"
_CATALOG_AREAS_ENV = "HULLFORGE_CATALOG_AREAS"

# The area that cannot be switched off. It holds the tools that answer "what is
# this project and what can you do", which is where every session starts and
# which the model needs before it can sensibly ask for anything else.
_ALWAYS_ON_AREA = "Core"

# Where the area travels to any client that wants it, not just this one.
#
# MCP's _meta is the sanctioned extension point and a domain-prefixed key is the
# convention. It is here because the docs generator needs the area too, and the
# alternative was a second hand-maintained category list in Python - which is
# exactly the drift this whole feature exists to remove. It had already
# happened: the generator's own list had 63 of 120 tools filed under "Other".
_AREA_META_KEY = "jigwright.com/area"

_LIST_AREAS_TOOL = types.Tool(
    # Core, like the editor tools it sits beside, so anything reading
    # _meta to group tools gets a complete answer and needs no special
    # case for the ones the sidecar synthesises.
    meta={_AREA_META_KEY: _ALWAYS_ON_AREA},
    name="hf_list_tool_areas",
    title="List Tool Areas",
    description=(
        "Every area of the editor HullForge covers, with how many tools each "
        "one holds and whether it is currently listed. Start here when you do "
        "not know which tool you need. If catalog mode is on, use "
        "hf_enable_tool_areas to bring an area's tools into the tool list."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    annotations=types.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True
    ),
)

_ENABLE_AREAS_TOOL = types.Tool(
    # Core, like the editor tools it sits beside, so anything reading
    # _meta to group tools gets a complete answer and needs no special
    # case for the ones the sidecar synthesises.
    meta={_AREA_META_KEY: _ALWAYS_ON_AREA},
    name="hf_enable_tool_areas",
    title="Enable Tool Areas",
    description=(
        "Add one or more areas' tools to the tool list. The list updates "
        "immediately; no restart is needed. Enable what the task needs rather "
        "than everything, because the whole point of catalog mode is that the "
        "tools you are not using are not in your context."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "areas": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Area names exactly as hf_list_tool_areas reports them."
                ),
            }
        },
        "required": ["areas"],
        "additionalProperties": False,
    },
    annotations=types.ToolAnnotations(
        # It changes which tools are listed and nothing in the project.
        read_only_hint=True, destructive_hint=False, idempotent_hint=True
    ),
)

_DISABLE_AREAS_TOOL = types.Tool(
    # Core, like the editor tools it sits beside, so anything reading
    # _meta to group tools gets a complete answer and needs no special
    # case for the ones the sidecar synthesises.
    meta={_AREA_META_KEY: _ALWAYS_ON_AREA},
    name="hf_disable_tool_areas",
    title="Disable Tool Areas",
    description=(
        "Remove one or more areas' tools from the tool list, freeing the "
        "context they occupy. The Core area cannot be disabled."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "areas": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Area names exactly as hf_list_tool_areas reports them.",
            }
        },
        "required": ["areas"],
        "additionalProperties": False,
    },
    annotations=types.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True
    ),
)


def _catalog_mode_enabled() -> bool:
    """Whether catalog mode is on, from the environment.

    Read on every call rather than cached at import, so a test can flip it and
    so a user who sets it and restarts the client gets what they asked for
    without a stale module-level value in the way.
    """
    raw = os.environ.get(_CATALOG_MODE_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _preloaded_areas() -> set[str]:
    """Areas to switch on at startup, from the environment.

    For the user who already knows they are working on materials today and does
    not want to spend a round trip saying so.
    """
    raw = os.environ.get(_CATALOG_AREAS_ENV, "")
    return {part.strip() for part in raw.split(",") if part.strip()}

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
        # Area name per tool, from the editor. Catalog mode gates on this and
        # nothing here guesses it: a tool with no area is filed under
        # Uncategorised by the editor, which is loud there and visible here.
        self._areas: dict[str, str] = {}
        self._area_counts: dict[str, int] = {}
        self._enabled_areas: set[str] = {_ALWAYS_ON_AREA} | _preloaded_areas()
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
                + (
                    " CATALOG MODE IS ON: only some areas' tools are listed. Call "
                    "hf_list_tool_areas to see every area HullForge covers, then "
                    "hf_enable_tool_areas to bring the ones this task needs into "
                    "the list. The list updates immediately."
                    if _catalog_mode_enabled()
                    else " hf_list_tool_areas gives an overview of everything "
                    "available, grouped by area."
                )
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

        # Counts come from what will actually be LISTED, not from the editor's
        # own totals. The editor counts every registered tool including the
        # internal ones the sidecar strips, so quoting its number would report
        # an area as holding one more tool than the model can see. Small, and
        # exactly the sort of quiet inaccuracy this product does not ship.
        counts: dict[str, int] = {}
        for area in areas.values():
            counts[area] = counts.get(area, 0) + 1

        # The editor's area list is still worth having: it is the authoritative
        # set of NAMES, so an area whose tools are all internal still appears
        # rather than silently not existing.
        reported = described.get("areas")
        if isinstance(reported, list):
            for entry in reported:
                if isinstance(entry, dict) and entry.get("name"):
                    counts.setdefault(str(entry["name"]), 0)

        self._area_counts = counts

        log.info(
            "catalog: %d tools (%d mutating) across %d areas%s",
            len(tools), len(mutating), len(self._area_counts),
            " [catalog mode]" if _catalog_mode_enabled() else "",
        )
        return tools

    # -- catalog mode ------------------------------------------------------

    def _visible_tools(self, tools: list[types.Tool]) -> list[types.Tool]:
        """The tools to list, after catalog mode has had its say.

        With catalog mode off this is every tool, which is the point: the
        default behaviour of this product is that the tool list IS the truth
        about what dispatches, and a mode that quietly hides two thirds of it
        has to be asked for.
        """
        if not _catalog_mode_enabled():
            return tools

        return [
            tool for tool in tools
            if self._areas.get(tool.name, "Uncategorised") in self._enabled_areas
        ]

    def _meta_tools(self) -> list[types.Tool]:
        """The always-present tools that are not the editor's.

        hf_list_tool_areas is offered whether or not catalog mode is on, because
        "what can this thing do" is a real question with a short answer. The two
        switches are offered only when they would do something: listing a tool
        that is a no-op is worse than not listing it.
        """
        if not _catalog_mode_enabled():
            return [_BRIDGE_STATUS_TOOL, _LIST_AREAS_TOOL]
        return [
            _BRIDGE_STATUS_TOOL,
            _LIST_AREAS_TOOL,
            _ENABLE_AREAS_TOOL,
            _DISABLE_AREAS_TOOL,
        ]

    def _known_areas(self) -> list[str]:
        return sorted(self._area_counts)

    def _list_areas_result(self) -> types.CallToolResult:
        enabled = _catalog_mode_enabled()
        total = sum(self._area_counts.values())
        listed = sum(
            count for area, count in self._area_counts.items()
            if not enabled or area in self._enabled_areas
        )

        areas = [
            {
                "name": area,
                "tool_count": count,
                "listed": (not enabled) or area in self._enabled_areas,
                "always_on": area == _ALWAYS_ON_AREA,
            }
            for area, count in sorted(self._area_counts.items())
        ]

        payload: dict[str, Any] = {
            "catalog_mode": enabled,
            "areas": areas,
            "area_count": len(areas),
            "tools_total": total,
            "tools_listed": listed,
        }

        if enabled:
            payload["how_to_use"] = (
                "Call hf_enable_tool_areas with the areas this task needs. The "
                "tool list updates immediately. Disable areas you have finished "
                "with to free the context back up."
            )
            lines = [
                f"Catalog mode is ON. {listed} of {total} tools are listed, "
                f"across {len(areas)} areas."
            ]
        else:
            payload["how_to_enable"] = (
                f"Catalog mode is off, so every tool is listed. Set "
                f"{_CATALOG_MODE_ENV}=1 in the connector's environment to turn it "
                f"on, and optionally {_CATALOG_AREAS_ENV}=Materials,Blueprints to "
                f"start with particular areas already listed. Core is always on."
            )
            lines = [
                f"Catalog mode is off: all {total} tools are listed, across "
                f"{len(areas)} areas."
            ]

        for area in areas:
            mark = "on " if area["listed"] else "off"
            suffix = " (always on)" if area["always_on"] else ""
            lines.append(f"  [{mark}] {area['name']:<18} {area['tool_count']:>3}{suffix}")

        if not enabled:
            lines.append("")
            lines.append(payload["how_to_enable"])

        return types.CallToolResult(
            content=[types.TextContent(type="text", text="\n".join(lines))],
            structured_content=payload,
        )

    async def _set_areas_result(
        self, args: dict[str, Any], *, enable: bool
    ) -> types.CallToolResult:
        raw = args.get("areas")
        if not isinstance(raw, list) or not raw:
            return _error(
                "'areas' is required and must be a non-empty array of area "
                "names. hf_list_tool_areas reports them."
            )

        wanted = [str(a).strip() for a in raw if str(a).strip()]
        known = set(self._known_areas())

        # Case-insensitive match, because an area name typed from memory with
        # the wrong case is a typo the tool can fix rather than an error the
        # caller has to.
        lowered = {a.lower(): a for a in known}
        resolved: list[str] = []
        unknown: list[str] = []
        for name in wanted:
            match = lowered.get(name.lower())
            if match:
                resolved.append(match)
            else:
                unknown.append(name)

        if unknown:
            return _error(
                f"No such area: {', '.join(unknown)}. "
                f"Known areas: {', '.join(sorted(known)) or 'none, no editor is connected'}. "
                f"Nothing was changed."
            )

        refused: list[str] = []
        if not enable:
            refused = [a for a in resolved if a == _ALWAYS_ON_AREA]
            resolved = [a for a in resolved if a != _ALWAYS_ON_AREA]

        before = set(self._enabled_areas)
        if enable:
            self._enabled_areas |= set(resolved)
        else:
            self._enabled_areas -= set(resolved)
        self._enabled_areas.add(_ALWAYS_ON_AREA)

        changed = self._enabled_areas != before
        if changed:
            await self._notify_tools_changed()

        listed = sum(
            count for area, count in self._area_counts.items()
            if area in self._enabled_areas
        )

        verb = "Enabled" if enable else "Disabled"
        lines = [
            f"{verb} {', '.join(resolved) if resolved else 'nothing'}. "
            f"{listed} of {sum(self._area_counts.values())} tools are now listed."
        ]
        if refused:
            lines.append(
                f"{_ALWAYS_ON_AREA} cannot be disabled: it holds the tools that "
                f"answer what this project is and what else is available."
            )
        if not changed:
            lines.append("Nothing changed; those areas were already in that state.")

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=" ".join(lines))],
            structured_content={
                "enabled_areas": sorted(self._enabled_areas),
                "tools_listed": listed,
                "tools_total": sum(self._area_counts.values()),
                "changed": changed,
                "refused": refused,
            },
        )

    async def _notify_tools_changed(self) -> None:
        session = self._mcp_session
        if session is None:
            return
        try:
            await session.send_tool_list_changed()
        except Exception as exc:  # noqa: BLE001 - never fail a call over a notification
            log.debug("could not send tools/list_changed: %s", exc)

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
        return types.ListToolsResult(
            tools=[*self._meta_tools(), *self._visible_tools(tools)]
        )

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

        if name == _LIST_AREAS_TOOL.name:
            # Refresh first: asking what areas exist when the catalog has never
            # been fetched should answer the question, not report zero areas.
            if not self._area_counts:
                try:
                    await self._refresh_catalog()
                except (SessionError, BridgeError) as exc:
                    return _error(str(exc))
            return self._list_areas_result()

        if name in (_ENABLE_AREAS_TOOL.name, _DISABLE_AREAS_TOOL.name):
            if not _catalog_mode_enabled():
                return _error(
                    f"Catalog mode is off, so every tool is already listed and "
                    f"'{name}' would do nothing. Set {_CATALOG_MODE_ENV}=1 in the "
                    f"connector's environment to turn it on."
                )
            if not self._area_counts:
                try:
                    await self._refresh_catalog()
                except (SessionError, BridgeError) as exc:
                    return _error(str(exc))
            return await self._set_areas_result(
                args, enable=(name == _ENABLE_AREAS_TOOL.name)
            )

        # A tool from an area that is not listed. Refused rather than run,
        # because the tool list has to be the truth about what is callable: a
        # hidden tool that works anyway means the list is advice, and everything
        # else in this product depends on it not being advice. The remedy is one
        # call and it is named.
        if _catalog_mode_enabled() and self._areas:
            area = self._areas.get(name)
            if area is not None and area not in self._enabled_areas:
                return _error(
                    f"'{name}' is in the '{area}' area, which is not currently "
                    f"listed. Call hf_enable_tool_areas with "
                    f"{{\"areas\": [\"{area}\"]}} and then call it again. "
                    f"Nothing was changed."
                )

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

            await self._notify_tools_changed()
            log.info("editor set changed (%d now); told the client to refresh",
                     len(current))

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
