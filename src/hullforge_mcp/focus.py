# Copyright (c) 2026 Jigwright. All rights reserved.
"""Bring the Unreal editor to the front for a window capture, then give focus back.

WHY THIS LIVES IN THE CONNECTOR AND NOT IN THE PLUGIN. Windows only lets a
process take the foreground if it is the foreground process, was started by
it, or received the last input. The editor is none of those while the person
is talking to their assistant, so a request from inside Unreal just flashes the
taskbar. This connector IS started by the assistant's app, which is the
foreground process at exactly that moment, so the same request from here is
allowed.

WHY FOCUS IS GIVEN BACK. Leaving the editor in front would send the person's
next keystrokes, meant for their assistant, into a running game or an editor
shortcut. So the previous foreground window is restored as soon as the capture
is taken, and the editor is in front for a fraction of a second.

Everything here is a no-op off Windows and reports what it did, so a caller can
say plainly when the editor could not be brought forward instead of returning
a stale picture.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    class _FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwnd", wintypes.HWND),
            ("dwFlags", wintypes.DWORD),
            ("uCount", wintypes.UINT),
            ("dwTimeout", wintypes.DWORD),
        ]

    _user32.FlashWindowEx.argtypes = [ctypes.POINTER(_FLASHWINFO)]

    _SW_RESTORE = 9
    _SW_MINIMIZE = 6
    _GW_OWNER = 4
    _ASFW_ANY = 0xFFFFFFFF
    _FLASHW_ALL = 3
    _FLASHW_TIMERNOFG = 12


@dataclass
class FocusResult:
    """What happened, in words a tool result can carry."""

    window_found: bool = False
    was_foreground: bool = False
    brought_forward: bool = False
    was_minimized: bool = False
    restored_previous: bool = False
    note: str = ""


def _title(hwnd: int) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_editor_window(pid: int) -> int | None:
    """The editor's main window: a visible, ownerless top-level window of pid.

    Prefers the one whose title names the Unreal Editor, because a PIE window
    or a floating tab can also be top-level and ownerless.
    """
    if not IS_WINDOWS or not pid:
        return None

    found: list[tuple[int, str]] = []

    def _cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and _user32.GetWindow(hwnd, _GW_OWNER) is None:
            title = _title(hwnd)
            if title and (_user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd)):
                found.append((hwnd, title))
        return True

    _user32.EnumWindows(_EnumWindowsProc(_cb), 0)
    if not found:
        return None
    for hwnd, title in found:
        if "Unreal Editor" in title:
            return hwnd
    return found[0][0]


def foreground_window() -> int | None:
    if not IS_WINDOWS:
        return None
    return _user32.GetForegroundWindow() or None


def _set_foreground(hwnd: int) -> bool:
    if _user32.GetForegroundWindow() == hwnd:
        return True
    _user32.AllowSetForegroundWindow(_ASFW_ANY)
    if _user32.SetForegroundWindow(hwnd) and _user32.GetForegroundWindow() == hwnd:
        return True

    # The documented fallback: share input state with the thread that owns the
    # foreground window for the length of the call. Still refused when Windows
    # has decided this process may not take focus, which is reported, not hidden.
    fg = _user32.GetForegroundWindow()
    fg_thread = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = _kernel32.GetCurrentThreadId()
    attached = bool(fg_thread) and fg_thread != me and _user32.AttachThreadInput(me, fg_thread, True)
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(me, fg_thread, False)
    return _user32.GetForegroundWindow() == hwnd


def bring_forward(pid: int, settle_seconds: float = 0.35) -> tuple[FocusResult, int | None, int | None]:
    """Bring the editor of pid to the front.

    Returns the result, the editor window and the window that was in front
    before, so restore() can hand focus back. settle_seconds gives the editor a
    few frames to redraw once it is in front, since a window that was hidden or
    throttled shows its last frame until then.
    """
    r = FocusResult()
    if not IS_WINDOWS:
        r.note = "Not on Windows, so the editor window was not brought forward."
        return r, None, None

    hwnd = find_editor_window(pid)
    if not hwnd:
        r.note = "No window of the editor process was found."
        return r, None, None
    r.window_found = True

    previous = _user32.GetForegroundWindow() or None
    if previous == hwnd:
        r.was_foreground = True
        return r, hwnd, None

    if _user32.IsIconic(hwnd):
        r.was_minimized = True
        _user32.ShowWindow(hwnd, _SW_RESTORE)

    r.brought_forward = _set_foreground(hwnd)
    if r.brought_forward:
        time.sleep(settle_seconds)
    else:
        r.note = ("Windows did not let the editor come to the front. "
                  "Its taskbar button is flashing.")
        flash(hwnd)
    return r, hwnd, previous


def restore(r: FocusResult, hwnd: int | None, previous: int | None) -> None:
    """Give focus back to the window that had it, and re-minimize if needed."""
    if not IS_WINDOWS or not hwnd or not r.brought_forward:
        return
    if previous:
        r.restored_previous = _set_foreground(previous)
    if r.was_minimized:
        _user32.ShowWindow(hwnd, _SW_MINIMIZE)


def flash(hwnd: int) -> None:
    if not IS_WINDOWS or not hwnd:
        return
    info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd, _FLASHW_ALL | _FLASHW_TIMERNOFG, 0, 0)
    _user32.FlashWindowEx(ctypes.byref(info))
