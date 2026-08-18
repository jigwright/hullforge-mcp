# Copyright (c) 2026 Jigwright. All rights reserved.
"""Wire framing for the HullForge editor bridge.

4-byte little-endian uint32 payload length, then that many bytes of UTF-8 JSON.
Must match Source/HullForge/Private/HullForgeFraming.cpp exactly.

The decoder is transport-free on purpose: it consumes byte chunks from
anywhere, so every framing edge case is testable without a socket.
"""

from __future__ import annotations

import struct

HEADER_BYTES = 4
DEFAULT_MAX_FRAME_BYTES = 64 * 1024 * 1024


class FramingError(Exception):
    """Unrecoverable stream fault. The connection must be closed."""


def encode_frame(body: str) -> bytes:
    """Encode one frame. Empty bodies are legal and produce a bare zero header."""
    payload = body.encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


class FrameDecoder:
    """Incremental decoder.

    ``feed`` accepts arbitrary chunk boundaries - a partial header, several
    whole frames, or a single byte - and returns whatever frames completed.

    A fault is sticky. Once raised, every later ``feed`` raises again, so a
    confused or hostile peer cannot resynchronise by sending more bytes.
    """

    def __init__(self, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        self._buffer = bytearray()
        self._max_frame_bytes = max_frame_bytes
        self._error: str | None = None

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    @property
    def has_error(self) -> bool:
        return self._error is not None

    def reset(self) -> None:
        self._buffer.clear()
        self._error = None

    def feed(self, chunk: bytes) -> list[str]:
        if self._error is not None:
            raise FramingError(self._error)

        if chunk:
            self._buffer.extend(chunk)

        frames: list[str] = []
        while True:
            if len(self._buffer) < HEADER_BYTES:
                return frames

            (payload_len,) = struct.unpack_from("<I", self._buffer, 0)

            # Check the cap before touching the buffer, so a bogus 2 GB prefix
            # costs nothing.
            if payload_len > self._max_frame_bytes:
                self._error = (
                    f"peer declared a {payload_len} byte payload, "
                    f"cap is {self._max_frame_bytes}"
                )
                raise FramingError(self._error)

            total = HEADER_BYTES + payload_len
            if len(self._buffer) < total:
                return frames

            payload = bytes(self._buffer[HEADER_BYTES:total])
            del self._buffer[:total]

            try:
                frames.append(payload.decode("utf-8"))
            except UnicodeDecodeError as exc:
                self._error = f"payload was not valid UTF-8: {exc}"
                raise FramingError(self._error) from exc
