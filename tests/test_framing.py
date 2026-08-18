"""Framing codec tests.

These mirror Source/HullForge/Private/Tests/HullForgeFraming.spec.cpp. If the
two ever disagree, one side has drifted and the wire is broken.
"""

from __future__ import annotations

import struct

import pytest

from hullforge_mcp.framing import (
    DEFAULT_MAX_FRAME_BYTES,
    FrameDecoder,
    FramingError,
    encode_frame,
)


def header(length: int) -> bytes:
    return struct.pack("<I", length)


class TestEncode:
    def test_header_is_little_endian(self):
        assert encode_frame("ab") == b"\x02\x00\x00\x00ab"

    def test_empty_body_is_a_bare_zero_header(self):
        assert encode_frame("") == b"\x00\x00\x00\x00"

    def test_multibyte_length_counts_bytes_not_characters(self):
        # A 3-character string of 3-byte code points is 9 bytes on the wire.
        body = "\u65e5\u672c\u8a9e"
        frame = encode_frame(body)
        assert struct.unpack_from("<I", frame, 0)[0] == 9
        assert len(frame) == 13


class TestHappyPath:
    def test_round_trip_single_frame(self):
        d = FrameDecoder()
        assert d.feed(encode_frame('{"k":1}')) == ['{"k":1}']
        assert d.pending_bytes == 0

    def test_three_frames_in_one_chunk_keep_order(self):
        d = FrameDecoder()
        blob = encode_frame("one") + encode_frame("two") + encode_frame("three")
        assert d.feed(blob) == ["one", "two", "three"]

    def test_zero_length_frame_is_an_empty_body(self):
        d = FrameDecoder()
        assert d.feed(header(0)) == [""]

    def test_empty_chunk_is_harmless(self):
        d = FrameDecoder()
        assert d.feed(b"") == []
        assert not d.has_error


class TestPartialDelivery:
    def test_header_split_across_chunks(self):
        d = FrameDecoder()
        frame = encode_frame("hi")
        assert d.feed(frame[:2]) == []
        assert d.pending_bytes == 2
        assert d.feed(frame[2:]) == ["hi"]

    def test_body_split_across_chunks(self):
        d = FrameDecoder()
        frame = encode_frame("hello")
        assert d.feed(frame[:6]) == []
        assert d.feed(frame[6:]) == ["hello"]

    def test_byte_at_a_time(self):
        d = FrameDecoder()
        blob = encode_frame("drip") + encode_frame("feed")
        out: list[str] = []
        for i in range(len(blob)):
            out.extend(d.feed(blob[i : i + 1]))
        assert out == ["drip", "feed"]

    def test_whole_frame_plus_partial_next(self):
        d = FrameDecoder()
        first = encode_frame("whole")
        blob = first + encode_frame("partial")
        assert d.feed(blob[: len(first) + 5]) == ["whole"]
        assert d.pending_bytes > 0
        assert d.feed(blob[len(first) + 5 :]) == ["partial"]


class TestHostileInput:
    def test_oversized_prefix_rejected_without_allocating(self):
        d = FrameDecoder(max_frame_bytes=1024)
        with pytest.raises(FramingError, match="cap is 1024"):
            d.feed(header(0x7FFFFFFF))
        assert d.has_error

    def test_frame_exactly_at_cap_is_accepted(self):
        d = FrameDecoder(max_frame_bytes=8)
        assert d.feed(encode_frame("12345678")) == ["12345678"]

    def test_one_byte_over_cap_rejected(self):
        d = FrameDecoder(max_frame_bytes=8)
        with pytest.raises(FramingError):
            d.feed(header(9))

    def test_fault_is_sticky(self):
        d = FrameDecoder(max_frame_bytes=16)
        with pytest.raises(FramingError):
            d.feed(header(999))
        # A well-formed frame must not resynchronise the stream.
        with pytest.raises(FramingError):
            d.feed(encode_frame("ok"))

    def test_reset_clears_the_fault(self):
        d = FrameDecoder(max_frame_bytes=16)
        with pytest.raises(FramingError):
            d.feed(header(999))
        d.reset()
        assert not d.has_error
        assert d.feed(encode_frame("ok")) == ["ok"]

    def test_invalid_utf8_raises(self):
        d = FrameDecoder()
        with pytest.raises(FramingError, match="UTF-8"):
            d.feed(header(2) + b"\xff\xfe")


class TestPayloadFidelity:
    def test_multibyte_utf8_round_trips(self):
        body = "caf\u00e9 \u65e5\u672c\u8a9e \U0001f680"
        d = FrameDecoder()
        assert d.feed(encode_frame(body)) == [body]

    def test_embedded_quotes_and_backslashes(self):
        body = r'{"path":"/Game/A\\B","q":"he said \"hi\""}'
        d = FrameDecoder()
        assert d.feed(encode_frame(body)) == [body]

    def test_one_megabyte_body_in_64k_chunks(self):
        body = ("0123456789ABCDEF" * (1024 * 1024 // 16))
        blob = encode_frame(body)
        d = FrameDecoder()
        out: list[str] = []
        for i in range(0, len(blob), 65536):
            out.extend(d.feed(blob[i : i + 65536]))
        assert out == [body]


def test_default_cap_matches_cpp():
    # Source/HullForge/Private/HullForgeFraming.h kDefaultMaxFrameBytes
    assert DEFAULT_MAX_FRAME_BYTES == 64 * 1024 * 1024
