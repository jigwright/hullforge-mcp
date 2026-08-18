# Copyright (c) 2026 Jigwright. All rights reserved.
"""Async client for the HullForge editor bridge.

Owns the socket, the handshake, and request/response correlation. Knows
nothing about MCP.

Concurrency note: the editor answers frames in the order it receives them on a
single connection, but nothing in the wire protocol forbids interleaving. We
serialise requests behind a lock and match on ``id`` anyway, so an
out-of-order or unsolicited frame is detected rather than silently mistaken
for the answer to the current call.
"""

from __future__ import annotations

import asyncio
import json
import itertools
import uuid
from typing import Any

from .framing import FrameDecoder, FramingError, encode_frame
from .session import SessionInfo

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_REQUEST_TIMEOUT = 180.0


class BridgeError(Exception):
    """Transport or protocol failure. Distinct from a tool reporting failure."""


class ToolFailed(Exception):
    """The tool ran and reported a definite failure. Safe to retry."""

    def __init__(self, message: str, tool: str = "") -> None:
        super().__init__(message)
        self.tool = tool


class ToolOutcomeUnknown(Exception):
    """The editor stopped waiting. The operation MAY have taken effect.

    Never retry blindly on this - verify current state first. This is the whole
    reason the wire protocol distinguishes "unknown" from "failed".
    """

    def __init__(self, message: str, tool: str = "") -> None:
        super().__init__(message)
        self.tool = tool


class BridgeClient:
    def __init__(
        self,
        info: SessionInfo,
        host: str = "127.0.0.1",
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._info = info
        self._host = host
        self._request_timeout = request_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = FrameDecoder()
        self._inbox: list[str] = []
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self.server_version: str = ""
        self.last_call_deduplicated: bool = False

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._info.port), timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise BridgeError(
                f"Could not connect to the HullForge bridge at "
                f"{self._host}:{self._info.port}. The session file may be stale - "
                f"is the editor still running? ({exc})"
            ) from exc

        ack = await self._exchange({"hello": self._info.token})
        if not ack.get("ok"):
            raise BridgeError(
                f"Handshake rejected: {ack.get('error', 'no reason given')}"
            )
        self.server_version = str(ack.get("version", ""))

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass
        self._reader = None
        self._writer = None
        self._decoder.reset()
        self._inbox.clear()

    async def call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a tool. Returns the ``result`` object on success.

        ``operation_id`` makes the call idempotent: if the editor already
        completed that operation, it replays the original result instead of
        running it again. Supply one for anything that mutates, so a retry
        after a dropped response cannot double-apply.

        Raises ToolFailed for a definite failure and ToolOutcomeUnknown for a
        timeout, which callers must treat differently.
        """
        async with self._lock:
            request_id = next(self._ids)
            payload: dict[str, Any] = {
                "id": request_id,
                "tool": tool,
                "args": args or {},
            }
            if operation_id:
                payload["operation_id"] = operation_id
            response = await self._exchange(payload)

        if response.get("id") != request_id:
            raise BridgeError(
                f"Response correlation mismatch: sent id {request_id}, "
                f"got {response.get('id')!r}. The stream is out of sync."
            )

        self.last_call_deduplicated = bool(response.get("deduplicated", False))

        status = response.get("status", "failed")
        if response.get("ok"):
            result = response.get("result")
            return result if isinstance(result, dict) else {}

        message = str(response.get("error", "no reason given"))
        if status == "unknown":
            raise ToolOutcomeUnknown(message, tool)
        raise ToolFailed(message, tool)

    @staticmethod
    def new_operation_id() -> str:
        return uuid.uuid4().hex

    # -- internals ---------------------------------------------------------

    async def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._writer is None or self._reader is None:
            raise BridgeError("Not connected.")

        self._writer.write(encode_frame(json.dumps(payload)))
        await self._writer.drain()

        frame = await self._read_frame()
        try:
            decoded = json.loads(frame)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"Editor sent malformed JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise BridgeError("Editor sent a non-object response.")
        return decoded

    async def _read_frame(self) -> str:
        if self._inbox:
            return self._inbox.pop(0)

        assert self._reader is not None
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self._reader.read(65536), self._request_timeout
                )
            except asyncio.TimeoutError as exc:
                raise BridgeError(
                    f"No response from the editor within {self._request_timeout}s. "
                    f"It may be blocked on a modal dialog. The operation may still "
                    f"be running - verify before retrying."
                ) from exc

            if not chunk:
                raise BridgeError("Editor closed the connection.")

            try:
                frames = self._decoder.feed(chunk)
            except FramingError as exc:
                raise BridgeError(f"Framing fault: {exc}") from exc

            if frames:
                self._inbox.extend(frames[1:])
                return frames[0]
