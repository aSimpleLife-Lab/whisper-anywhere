from __future__ import annotations

import sys
import winreg
from pathlib import Path

APP_RUN_VALUE_NAME = "Whisper Anywhere"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


class StartupManagerError(RuntimeError):
    pass


class StartupManager:
    def __init__(self, app_path: str | Path | None = None) -> None:
        self.app_path = Path(app_path) if app_path else Path(sys.executable)

    def startup_command(self) -> str:
        if getattr(sys, "frozen", False) or self.app_path.suffix.lower() == ".exe":
            return f'"{self.app_path}" --minimized'
        script_path = Path(sys.argv[0]).resolve()
        return f'"{self.app_path}" "{script_path}" --minimized'

    def is_enabled(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
                value, _value_type = winreg.QueryValueEx(key, APP_RUN_VALUE_NAME)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StartupManagerError("Could not read the Windows startup setting.") from exc
        return str(value) == self.startup_command()

    def set_enabled(self, enabled: bool) -> None:
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, APP_RUN_VALUE_NAME, 0, winreg.REG_SZ, self.startup_command())
                else:
                    try:
                        winreg.DeleteValue(key, APP_RUN_VALUE_NAME)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            action = "enable" if enabled else "disable"
            raise StartupManagerError(f"Could not {action} startup with Windows.") from exc

    def sync(self, enabled: bool) -> None:
        self.set_enabled(enabled)
