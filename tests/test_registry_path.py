# Copyright (c) 2026 Jigwright. All rights reserved.
"""The registry path must match Unreal's, on every platform.

WHY THIS FILE EXISTS
--------------------
The connector and the editor are separate processes that agree on one thing: a
directory path. If they disagree, the connector starts cleanly, reports no
editors, and the editor insists it is advertising. Nothing errors on either
side, so nothing is logged, and the user sees "connects but does nothing" -
which is the single most common complaint about tools in this category.

The first implementation was wrong on macOS and Linux and right on Windows,
which is the worst combination: it passed every test on the only machine anyone
ran it on. These tests fake the platform so the mismatch cannot hide again.

Ground truth, read from the engine source rather than assumed:

    FWindowsPlatformProcess::UserSettingsDir  FOLDERID_LocalAppData, no suffix
    FApplePlatformProcess::ApplicationSettingsDir
                                              Application Support + "/Epic/"
    FUnixPlatformProcess::ApplicationSettingsDir
                                              $HOME + "/.config/Epic/"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hullforge_mcp import session


class TestRegistryPathMatchesUnreal:
    def test_windows_has_no_vendor_directory(self, monkeypatch):
        # Windows is the one platform where Unreal does NOT append Epic/. Getting
        # this wrong the other way would break the only configuration that
        # currently works.
        monkeypatch.setattr(session.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Test\AppData\Local")

        result = session.session_registry_dir()

        assert result.parts[-2:] == ("HullForge", "sessions")
        assert "Epic" not in result.parts

    def test_macos_uses_application_support_under_epic(self, monkeypatch):
        monkeypatch.setattr(session.sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/test")))

        result = session.session_registry_dir()

        assert result == Path(
            "/Users/test/Library/Application Support/Epic/HullForge/sessions"
        )

    def test_linux_uses_config_under_epic(self, monkeypatch):
        monkeypatch.setattr(session.sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))

        result = session.session_registry_dir()

        assert result == Path("/home/test/.config/Epic/HullForge/sessions")

    def test_linux_ignores_xdg_config_home(self, monkeypatch):
        """Unreal builds this from the home directory and never consults XDG.

        Honouring XDG_CONFIG_HOME here would look more correct and be more
        wrong: on any machine that sets it, the connector would look somewhere
        the editor never writes.
        """
        monkeypatch.setattr(session.sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
        monkeypatch.setenv("XDG_CONFIG_HOME", "/somewhere/else")

        result = session.session_registry_dir()

        assert result == Path("/home/test/.config/Epic/HullForge/sessions")
        assert "somewhere" not in str(result)

    @pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
    def test_every_platform_ends_the_same_way(self, monkeypatch, platform):
        """Only the base differs. The last two components never do."""
        monkeypatch.setattr(session.sys, "platform", platform)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
        monkeypatch.setenv("LOCALAPPDATA", "/home/test/AppData/Local")

        result = session.session_registry_dir()

        assert result.parts[-2:] == ("HullForge", "sessions")

    @pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
    def test_result_is_under_the_platform_base(self, monkeypatch, platform):
        """The base differs per platform; the result is always beneath it.

        This replaced an is_absolute() check, which failed for the wrong reason:
        Path("/home/test").is_absolute() is False on Windows because there is no
        drive letter, so the test was asserting Windows path semantics about
        POSIX fixture paths rather than anything about this function.
        """
        monkeypatch.setattr(session.sys, "platform", platform)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
        monkeypatch.setenv("LOCALAPPDATA", "/home/test/AppData/Local")

        result = session.session_registry_dir()

        assert "home" in result.parts and "test" in result.parts

    def test_windows_falls_back_to_home_without_localappdata(self, monkeypatch):
        """LOCALAPPDATA is effectively always set, but an empty string is not
        the same as unset, and os.environ.get returns it happily."""
        monkeypatch.setattr(session.sys, "platform", "win32")
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))

        result = session.session_registry_dir()

        assert result == Path("/home/test/HullForge/sessions")
