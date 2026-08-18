"""Shared fixtures.

Almost everything here runs against a fake editor on a loopback socket, so the
suite needs no Unreal install. Tests that genuinely require a live editor are
marked ``integration`` and skipped by default.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import pytest

from hullforge_mcp.framing import FrameDecoder, encode_frame
from hullforge_mcp.session import SessionInfo

TEST_TOKEN = "test-token-abc123"


class FakeEditor:
    """Minimal stand-in for the C++ bridge.

    Speaks the same wire protocol: token handshake, then framed request ->
    framed response. Handlers are supplied per-test so failure modes are easy
    to provoke.
    """

    def __init__(self, token: str = TEST_TOKEN) -> None:
        self.token = token
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.received: list[dict[str, Any]] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        # Set to send a raw byte sequence instead of a proper frame.
        self.raw_override: bytes | None = None
        self.drop_after_handshake = False
        # Tools named here get no reply at all, to exercise client timeouts.
        self.silent_tools: set[str] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    def tool(self, name: str):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Close live connections BEFORE the server. Since Python 3.12,
        # Server.wait_closed() waits for handler tasks to finish, and a handler
        # parked on reader.read() with a client still attached never will -
        # teardown hangs rather than failing, which is miserable to diagnose.
        for w in list(self._writers):
            try:
                w.close()
            except Exception:
                pass
        self._writers.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def session(self) -> SessionInfo:
        return SessionInfo(port=self.port, token=self.token, pid=0)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        decoder = FrameDecoder()
        authed = False
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                for frame in decoder.feed(chunk):
                    msg = json.loads(frame)

                    if not authed:
                        if msg.get("hello") != self.token:
                            writer.write(encode_frame(json.dumps(
                                {"id": None, "ok": False, "status": "failed",
                                 "error": "Auth required."})))
                            await writer.drain()
                            writer.close()
                            return
                        authed = True
                        writer.write(encode_frame(json.dumps(
                            {"ok": True, "status": "ok", "server": "hullforge",
                             "version": "0.1.0", "protocol": 1})))
                        await writer.drain()
                        if self.drop_after_handshake:
                            writer.close()
                            return
                        continue

                    self.received.append(msg)

                    if self.raw_override is not None:
                        writer.write(self.raw_override)
                        await writer.drain()
                        continue

                    tool = msg.get("tool", "")
                    if tool in self.silent_tools:
                        continue  # deliberately never answer
                    handler = self.handlers.get(tool)
                    if handler is None:
                        reply = {"id": msg.get("id"), "ok": False, "status": "failed",
                                 "error": f"Unknown tool '{tool}'."}
                    else:
                        reply = handler(msg)
                    writer.write(encode_frame(json.dumps(reply)))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        finally:
            self._writers.discard(writer)


@pytest.fixture
async def editor():
    ed = FakeEditor()
    await ed.start()
    try:
        yield ed
    finally:
        await ed.stop()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs a live Unreal editor running HullForge"
    )
    config.addinivalue_line(
        "markers",
        "live: needs a real editor with RHI and revision control; writes to the "
        "project. Opt-in via HULLFORGE_LIVE=1.",
    )
