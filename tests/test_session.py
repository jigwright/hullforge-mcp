"""Session discovery tests."""

from __future__ import annotations

import json
import os

import pytest

from hullforge_mcp.session import (
    SessionError,
    discover,
    process_is_alive,
    read_session,
    session_file_path,
)


def write(tmp_path, payload, name="session.json"):
    p = tmp_path / name
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                 encoding="utf-8")
    return p


class TestPath:
    def test_resolves_under_saved_hullforge(self):
        p = session_file_path(r"C:\proj")
        assert p.parts[-3:] == ("Saved", "HullForge", "session.json")


class TestRead:
    def test_round_trips_every_field(self, tmp_path):
        p = write(tmp_path, {
            "port": 8765, "token": "abc", "protocol": 1,
            "project_name": "ExampleProject", "pid": 4242,
        })
        info = read_session(p)
        assert (info.port, info.token, info.protocol) == (8765, "abc", 1)
        assert info.project_name == "ExampleProject"
        assert info.pid == 4242

    def test_defaults_when_optional_fields_absent(self, tmp_path):
        p = write(tmp_path, {"port": 1, "token": "t"})
        info = read_session(p)
        assert info.protocol == 1 and info.pid == 0 and info.project_name == ""

    def test_missing_file_mentions_the_editor(self, tmp_path):
        with pytest.raises(SessionError, match="editor"):
            read_session(tmp_path / "nope.json")

    def test_malformed_json(self, tmp_path):
        p = write(tmp_path, "{ not json")
        with pytest.raises(SessionError, match="not valid JSON"):
            read_session(p)

    def test_top_level_array_rejected(self, tmp_path):
        p = write(tmp_path, "[1,2,3]")
        with pytest.raises(SessionError, match="not a JSON object"):
            read_session(p)

    @pytest.mark.parametrize("payload,match", [
        ({"token": "t"}, "port"),
        ({"port": 0, "token": "t"}, "port"),
        ({"port": -1, "token": "t"}, "port"),
        ({"port": "8765", "token": "t"}, "port"),
        ({"port": 8765}, "token"),
        ({"port": 8765, "token": ""}, "token"),
        ({"port": 8765, "token": 123}, "token"),
    ])
    def test_rejects_unusable_fields(self, tmp_path, payload, match):
        p = write(tmp_path, payload)
        with pytest.raises(SessionError, match=match):
            read_session(p)


class TestLiveness:
    def test_current_process_is_alive(self):
        assert process_is_alive(os.getpid())

    def test_unknown_pid_is_treated_as_alive(self):
        # An old session file without a pid should not be rejected outright;
        # the connect attempt is the real test.
        assert process_is_alive(0)

    def test_almost_certainly_dead_pid(self):
        # Not guaranteed free, but a miss here means the check works and the
        # pid was reused - vanishingly unlikely on a dev box.
        assert not process_is_alive(999999)


class TestDiscover:
    def test_accepts_a_live_session(self, tmp_path):
        d = tmp_path / "Saved" / "HullForge"
        d.mkdir(parents=True)
        (d / "session.json").write_text(json.dumps(
            {"port": 8765, "token": "t", "pid": os.getpid()}), encoding="utf-8")
        assert discover(tmp_path).port == 8765

    def test_rejects_a_stale_session_and_says_so(self, tmp_path):
        # The exact case we hit by force-killing the editor: file survives,
        # port is dead. The message must point at the real cause.
        d = tmp_path / "Saved" / "HullForge"
        d.mkdir(parents=True)
        (d / "session.json").write_text(json.dumps(
            {"port": 8765, "token": "t", "pid": 999999}), encoding="utf-8")
        with pytest.raises(SessionError, match="Stale"):
            discover(tmp_path)
