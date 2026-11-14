"""Explaining a lost editor: crash folder parsing and the loss message."""

from __future__ import annotations

import os
import time

import mcp.types as types
import pytest

from hullforge_mcp import crash
from hullforge_mcp.server import HullForgeServer

CONTEXT = """<?xml version="1.0" encoding="UTF-8"?>
<FGenericCrashContext>
 <RuntimeProperties>
  <CrashType>Crash</CrashType>
  <ErrorMessage>Assertion failed: IsValid(Actor) [File:D:\\Game\\Source\\Door.cpp] [Line: 42]</ErrorMessage>
  <CallStack>UnrealEditor_Game!ADoor::Open() [D:\\Game\\Source\\Door.cpp:42]
UnrealEditor_Game!ADoor::Tick() [D:\\Game\\Source\\Door.cpp:17]
UnrealEditor_Engine!AActor::TickActor()</CallStack>
 </RuntimeProperties>
</FGenericCrashContext>
"""


def make_crash(project, name="UECC-Windows-ABC", age=0.0):
    folder = project / "Saved" / "Crashes" / name
    folder.mkdir(parents=True)
    (folder / "CrashContext.runtime-xml").write_text(CONTEXT, encoding="utf-8")
    (folder / "Game.log").write_text(
        "LogTemp: fine\nLogOutputDevice: Error: Assertion failed: IsValid(Actor)\n", encoding="utf-8")
    t = time.time() - age
    os.utime(folder, (t, t))
    return folder


def test_reads_error_stack_and_log(tmp_path):
    make_crash(tmp_path)
    r = crash.find_latest_crash(str(tmp_path), time.time() - 60)
    assert r is not None
    assert r["error"].startswith("Assertion failed: IsValid(Actor)")
    assert r["callstack"][0].startswith("UnrealEditor_Game!ADoor::Open()")
    assert len(r["callstack"]) == 3
    assert any("Assertion failed" in ln for ln in r["log_errors"])
    assert r["type"] == "Crash"


def test_an_ensure_does_not_explain_a_lost_editor(tmp_path):
    folder = make_crash(tmp_path)
    ctx = folder / "CrashContext.runtime-xml"
    ctx.write_text(CONTEXT.replace("<CrashType>Crash</CrashType>", "<CrashType>Ensure</CrashType>"), encoding="utf-8")
    assert crash.find_latest_crash(str(tmp_path), time.time() - 60) is None


def test_error_message_drops_its_embedded_stack(tmp_path):
    folder = make_crash(tmp_path)
    ctx = folder / "CrashContext.runtime-xml"
    ctx.write_text(CONTEXT.replace("[Line: 42]</ErrorMessage>",
                                   "[Line: 42] \nStack: \n0x00007ffd UnrealEditor-Core.dll!UnknownFunction []</ErrorMessage>"),
                   encoding="utf-8")
    r = crash.find_latest_crash(str(tmp_path), time.time() - 60)
    assert "UnknownFunction" not in r["error"]
    assert r["error"].endswith("[Line: 42]")


def test_ignores_crashes_from_before_the_connection(tmp_path):
    make_crash(tmp_path, age=3600)
    assert crash.find_latest_crash(str(tmp_path), time.time()) is None


def test_no_crash_folder_reads_as_closed():
    text = crash.describe_loss("Game", None)
    assert "closed or killed" in text and "CRASHED" not in text


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_failed_call_explains_a_crash(tmp_path, monkeypatch):
    from hullforge_mcp import server as server_mod

    make_crash(tmp_path)
    srv = HullForgeServer(tmp_path)
    srv._last_editor = {"pid": 4242, "project": "Game", "project_dir": str(tmp_path),
                        "connected_at": time.time() - 60}
    monkeypatch.setattr(server_mod, "process_is_alive", lambda pid: False)
    monkeypatch.setattr(server_mod, "discover_all", lambda _dir=None: [])

    result = await srv._dispatch("hf_ping", {})
    text = result.content[0].text
    assert result.is_error
    assert "CRASHED" in text and "IsValid(Actor)" in text
    assert result.structured_content["editor_lost"]["crashed"] is True

    status = srv._bridge_status()
    assert "CRASHED" in status.content[0].text
    assert status.structured_content["last_editor_lost"]["pid"] == 4242


async def test_live_editor_is_never_reported_lost(tmp_path, monkeypatch):
    from hullforge_mcp import server as server_mod

    make_crash(tmp_path)
    srv = HullForgeServer(tmp_path)
    srv._last_editor = {"pid": 4242, "project": "Game", "project_dir": str(tmp_path),
                        "connected_at": time.time() - 60}
    monkeypatch.setattr(server_mod, "process_is_alive", lambda pid: True)
    assert srv._editor_loss() is None
