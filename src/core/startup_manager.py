from __future__ import annotations

import os
import re
import sys
import winreg
from pathlib import Path

APP_RUN_VALUE_NAME = "Whisper Anywhere"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Task Manager's Startup apps switch, by the same value name: an odd first byte
# (03) is disabled, an even one (02) enabled, and no value at all is enabled.
APPROVED_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APPROVED_ENABLED = bytes([2]) + bytes(11)  # exactly what Task Manager writes for Enable


class StartupManagerError(RuntimeError):
    pass


def _programs(command: str) -> list[str]:
    """The exe (and from source, the script) a Run command starts, comparable."""
    paths = re.findall(r'"([^"]+)"', command) or command.split()[:1]
    return [os.path.normcase(os.path.normpath(path)) for path in paths]


class StartupManager:
    """The one Run value every copy shares (installed, portable, from source):
    a copy takes it only when it is missing, already its own, or starts an exe
    that is gone."""

    def __init__(self, app_path: str | Path | None = None) -> None:
        self.app_path = Path(app_path) if app_path else Path(sys.executable)

    def startup_command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{self.app_path}" --minimized'
        # From source the interpreter runs the script (python.exe alone exits
        # at sign-in); pythonw.exe keeps a console window from flashing up.
        pythonw = self.app_path.with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else self.app_path
        script_path = Path(sys.argv[0]).resolve()
        return f'"{interpreter}" "{script_path}" --minimized'

    def state(self) -> str:
        """"on", "off", "disabled" (turned off in Task Manager) or "other" (it
        starts another copy that is still there). Only reads."""
        command = self._run_command()
        if command is None:
            return "off"
        ours = self._is_ours(command)
        if not ours and self._starts_existing_copy(command):
            return "other"
        if self._disabled_in_task_manager():
            return "disabled"
        return "on" if ours else "off"

    def set_enabled(self, enabled: bool) -> None:
        """The switch. On: this copy's Run value, and Task Manager's "disabled"
        cleared, since the user asked. Off: the value goes only if it is ours."""
        try:
            if enabled:
                self._write_run_value()
                if self._disabled_in_task_manager():
                    with winreg.CreateKeyEx(
                        winreg.HKEY_CURRENT_USER, APPROVED_KEY_PATH, 0, winreg.KEY_SET_VALUE
                    ) as key:
                        winreg.SetValueEx(
                            key, APP_RUN_VALUE_NAME, 0, winreg.REG_BINARY, APPROVED_ENABLED
                        )
            elif self._is_ours(self._run_command() or ""):
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, APP_RUN_VALUE_NAME)
        except OSError as exc:
            action = "enable" if enabled else "disable"
            raise StartupManagerError(
                f"Could not {action} startup with Windows."
            ) from exc

    def sync_at_start(self, wanted: bool) -> str:
        """At each start, with "Start with Windows" wanted: point the value at
        this copy only if it is missing, already ours, or its exe is gone. Never
        re-enables what Task Manager turned off and never takes the value from
        another copy that is still there. Returns state()."""
        if wanted:
            command = self._run_command()
            if command != self.startup_command() and (
                command is None
                or self._is_ours(command)
                or not self._starts_existing_copy(command)
            ):
                try:
                    self._write_run_value()
                except OSError as exc:
                    raise StartupManagerError(
                        "Could not enable startup with Windows."
                    ) from exc
        return self.state()

    def _read(self, key_path: str) -> object:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ
            ) as key:
                value, _value_type = winreg.QueryValueEx(key, APP_RUN_VALUE_NAME)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StartupManagerError(
                "Could not read the Windows startup setting."
            ) from exc
        return value

    def _run_command(self) -> str | None:
        command = self._read(RUN_KEY_PATH)
        return command if isinstance(command, str) and command.strip() else None

    def _write_run_value(self) -> None:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(
                key, APP_RUN_VALUE_NAME, 0, winreg.REG_SZ, self.startup_command()
            )

    def _disabled_in_task_manager(self) -> bool:
        flag = self._read(APPROVED_KEY_PATH)
        return isinstance(flag, bytes) and len(flag) > 0 and flag[0] % 2 == 1

    def _is_ours(self, command: str) -> bool:
        return _programs(command) == _programs(self.startup_command())

    @staticmethod
    def _starts_existing_copy(command: str) -> bool:
        programs = _programs(command)
        return bool(programs) and all(Path(program).is_file() for program in programs)
