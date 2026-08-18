# Copyright (c) 2026 Jigwright. All rights reserved.
"""Discovery of a running HullForge editor.

The editor writes Saved/HullForge/session.json on startup and deletes it on
clean shutdown. A crash leaves it behind pointing at a dead port, so the file
alone is not evidence that anything is listening - hence the pid liveness
check. Treat a failed connect as "stale session", not "server broken".
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


class SessionError(Exception):
    """No usable session. The message is written to be shown to a human."""


@dataclass(frozen=True)
class SessionInfo:
    port: int
    token: str
    protocol: int = 1
    project_name: str = ""
    pid: int = 0
    project_dir: str = ""


def session_file_path(project_dir: str | os.PathLike[str]) -> Path:
    return Path(project_dir) / "Saved" / "HullForge" / "session.json"


def process_is_alive(pid: int) -> bool:
    """Best-effort liveness check.

    Returns True when pid is unknown (0), because an old session file without a
    pid should not be rejected outright - the connect attempt is the real test.
    """
    if pid <= 0:
        return True

    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, we just cannot signal it
    return True


def read_session(path: str | os.PathLike[str]) -> SessionInfo:
    """Parse and validate a session file. Raises SessionError with a human message."""
    p = Path(path)

    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SessionError(
            f"No HullForge session file at {p}. Is the Unreal editor running "
            f"with the HullForge plugin enabled?"
        ) from exc
    except OSError as exc:
        raise SessionError(f"Could not read {p}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SessionError(f"Session file {p} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SessionError(f"Session file {p} is not a JSON object.")

    port = data.get("port")
    token = data.get("token")
    if not isinstance(port, int) or port <= 0:
        raise SessionError(f"Session file {p} has no usable port.")
    if not isinstance(token, str) or not token:
        raise SessionError(f"Session file {p} has no token.")

    return SessionInfo(
        port=port,
        token=token,
        protocol=int(data.get("protocol", 1)),
        project_name=str(data.get("project_name", "")),
        pid=int(data.get("pid", 0)),
        project_dir=str(data.get("project_dir", "")),
    )


def session_registry_dir() -> Path:
    """Where editors advertise themselves, user-globally.

    Mirrors HullForge::GetSessionRegistryDir on the C++ side, which builds this
    from FPlatformProcess::UserSettingsDir(). Scanning it needs no configuration
    at all, which is what makes the connector distributable: requiring every
    user to set HULLFORGE_PROJECT_DIR is hostile, and it cannot describe two
    editors open at once.

    The two sides MUST agree exactly. They are separate processes, often
    separate languages, and a mismatch produces the worst possible symptom: the
    connector starts fine, reports no editors, and the editor insists it is
    advertising. Nothing is logged because nothing went wrong on either side.

    Unreal's UserSettingsDir is not the same shape on every platform, and the
    difference is not the obvious one:

        Windows   %LOCALAPPDATA%                       (no vendor suffix)
        macOS     ~/Library/Application Support/Epic/   (vendor suffix)
        Linux     ~/.config/Epic/                       (vendor suffix)

    An earlier version of this function used ~/.config on everything that was
    not Windows. That is wrong twice over: the wrong base directory on macOS,
    and a missing Epic/ component on both. It would have shipped as "works on
    Windows, silently finds nothing anywhere else".
    """
    if sys.platform == "win32":
        # FWindowsPlatformProcess::UserSettingsDir returns FOLDERID_LocalAppData
        # directly, with no vendor directory appended.
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    elif sys.platform == "darwin":
        # FApplePlatformProcess::ApplicationSettingsDir is the Application
        # Support folder with "/Epic/" appended.
        base = Path.home() / "Library" / "Application Support" / "Epic"
    else:
        # FUnixPlatformProcess::ApplicationSettingsDir is $HOME/.config/Epic/.
        # Note it uses the home directory directly and ignores XDG_CONFIG_HOME,
        # so honouring XDG here would reintroduce the mismatch on any machine
        # that sets it.
        base = Path.home() / ".config" / "Epic"

    return base / "HullForge" / "sessions"


def discover_all(project_dir: str | os.PathLike[str] | None = None) -> list[SessionInfo]:
    """Every editor currently advertising itself, newest first.

    Sources, in order:
      1. the user-global registry (no configuration needed)
      2. project_dir's own Saved/HullForge/session.json, if given

    Dead entries are filtered by pid, so a crashed editor does not masquerade
    as a live one.
    """
    found: dict[tuple[int, str], SessionInfo] = {}

    registry = session_registry_dir()
    if registry.is_dir():
        for path in sorted(registry.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                info = read_session(path)
            except SessionError:
                continue  # unreadable or half-written; not our problem
            if process_is_alive(info.pid):
                found.setdefault((info.port, info.token), info)

    if project_dir:
        try:
            info = read_session(session_file_path(project_dir))
            if process_is_alive(info.pid):
                found.setdefault((info.port, info.token), info)
        except SessionError:
            pass

    return list(found.values())


def discover(project_dir: str | os.PathLike[str]) -> SessionInfo:
    """Read the session file and reject it if the editor that wrote it is gone."""
    path = session_file_path(project_dir)
    info = read_session(path)

    if not process_is_alive(info.pid):
        raise SessionError(
            f"Stale HullForge session: {path} names pid {info.pid}, which is no "
            f"longer running. The editor was probably force-killed or crashed. "
            f"Start the editor again, or delete the file."
        )

    return info
