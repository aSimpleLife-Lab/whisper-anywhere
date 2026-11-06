from __future__ import annotations

import sys
from pathlib import Path

import pytest

import core.startup_manager as startup_module
from core.startup_manager import StartupManager

DISABLED = bytes([3, 0, 0, 0]) + bytes.fromhex("5ee00e289710dd01")  # Task Manager, 2026-07-10
ENABLED = bytes([2]) + bytes(11)  # what Task Manager writes when you click Enable


class FakeRegistry:
    """Stands in for winreg: no test touches the real Run key or StartupApproved."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_BINARY = 3

    class Key:
        def __init__(self, path: str) -> None:
            self.path = path.lower()

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}

    def OpenKey(self, root, path, reserved=0, access=1):
        return self.Key(path)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        try:
            return self.values[(key.path, name.lower())], 0
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, reserved, kind, value) -> None:
        self.values[(key.path, name.lower())] = value

    def DeleteValue(self, key, name) -> None:
        if self.values.pop((key.path, name.lower()), None) is None:
            raise FileNotFoundError(name)

    def run_value(self):
        return self.values.get((startup_module.RUN_KEY_PATH.lower(), "whisper anywhere"))

    def approved(self):
        return self.values.get((startup_module.APPROVED_KEY_PATH.lower(), "whisper anywhere"))

    def set_run(self, command: str) -> None:
        self.values[(startup_module.RUN_KEY_PATH.lower(), "whisper anywhere")] = command

    def set_approved(self, flag: bytes) -> None:
        self.values[(startup_module.APPROVED_KEY_PATH.lower(), "whisper anywhere")] = flag


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    fake = FakeRegistry()
    monkeypatch.setattr(startup_module, "winreg", fake)
    return fake


@pytest.fixture
def frozen_copy(tmp_path, monkeypatch: pytest.MonkeyPatch) -> StartupManager:
    exe = tmp_path / "F" / "Whisper Anywhere" / "Whisper Anywhere.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    return StartupManager(exe)


def other_copy(tmp_path, exists: bool = True) -> str:
    exe = tmp_path / "Programs" / "Whisper Anywhere" / "Whisper Anywhere.exe"
    if exists:
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
    return f'"{exe}" --minimized'


def test_a_source_run_starts_pythonw_with_the_script(tmp_path, monkeypatch) -> None:
    # From source, python.exe ends in .exe too: the Run key got
    # "python.exe --minimized", which exits at sign-in with "unknown option".
    (tmp_path / "pythonw.exe").write_bytes(b"")
    script = tmp_path / "src" / "main.py"
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "argv", [str(script)])

    command = StartupManager(tmp_path / "python.exe").startup_command()

    assert command == f'"{tmp_path / "pythonw.exe"}" "{Path(script).resolve()}" --minimized'


def test_turned_off_in_task_manager_reads_as_disabled_and_a_start_never_turns_it_back_on(
    registry: FakeRegistry, frozen_copy: StartupManager
) -> None:
    # H1: Task Manager had marked the entry disabled since 2026-07-10, the app
    # never read that flag, and its switch kept saying on.
    registry.set_run(frozen_copy.startup_command())
    registry.set_approved(DISABLED)

    assert frozen_copy.state() == "disabled"
    assert frozen_copy.sync_at_start(True) == "disabled"
    assert registry.approved() == DISABLED

    frozen_copy.set_enabled(True)  # the user's own switch may turn it back on

    assert registry.approved() == ENABLED
    assert frozen_copy.state() == "on"


@pytest.mark.parametrize("flag", [ENABLED, None])
def test_enabled_or_never_touched_in_task_manager_reads_as_on(
    registry: FakeRegistry, frozen_copy: StartupManager, flag
) -> None:
    registry.set_run(frozen_copy.startup_command().upper())  # names and paths ignore case
    if flag is not None:
        registry.set_approved(flag)

    assert frozen_copy.state() == "on"


def test_another_copy_keeps_the_value_while_its_exe_is_there(
    registry: FakeRegistry, frozen_copy: StartupManager, tmp_path
) -> None:
    # PL-2: whichever copy started last (a source run, the F: copy, the
    # installed one) took the single Run value over.
    command = other_copy(tmp_path)
    registry.set_run(command)

    assert frozen_copy.state() == "other"
    assert frozen_copy.sync_at_start(True) == "other"
    frozen_copy.set_enabled(False)
    assert registry.run_value() == command

    frozen_copy.set_enabled(True)  # the user asked for this copy
    assert registry.run_value() == frozen_copy.startup_command()


def test_a_value_whose_exe_is_gone_is_pointed_at_this_copy(
    registry: FakeRegistry, frozen_copy: StartupManager, tmp_path
) -> None:
    registry.set_run(other_copy(tmp_path, exists=False))

    assert frozen_copy.state() == "off"
    assert frozen_copy.sync_at_start(True) == "on"
    assert registry.run_value() == frozen_copy.startup_command()


def test_a_start_writes_a_missing_value_only_when_wanted(
    registry: FakeRegistry, frozen_copy: StartupManager
) -> None:
    assert frozen_copy.sync_at_start(False) == "off"
    assert registry.run_value() is None

    assert frozen_copy.sync_at_start(True) == "on"
    assert registry.run_value() == frozen_copy.startup_command()
    assert registry.approved() is None  # nothing written that Task Manager owns


def test_a_start_refreshes_its_own_value_and_keeps_the_task_manager_flag(
    registry: FakeRegistry, frozen_copy: StartupManager
) -> None:
    registry.set_run(f'"{frozen_copy.app_path}"')  # ours, written by an older version
    registry.set_approved(DISABLED)

    assert frozen_copy.sync_at_start(True) == "disabled"
    assert registry.run_value() == frozen_copy.startup_command()
    assert registry.approved() == DISABLED


def test_switching_off_removes_only_this_copys_value(
    registry: FakeRegistry, frozen_copy: StartupManager
) -> None:
    frozen_copy.set_enabled(True)
    assert frozen_copy.state() == "on"

    frozen_copy.set_enabled(False)

    assert registry.run_value() is None
    assert frozen_copy.state() == "off"
