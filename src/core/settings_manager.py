from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

APP_NAME = "Whisper Anywhere"


def _appdata_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return Path.home() / "AppData" / "Roaming"


def _localappdata_root() -> Path:
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        return Path(localappdata)
    return Path.home() / "AppData" / "Local"


def default_model_path() -> str:
    return str(_localappdata_root() / APP_NAME / "models")


DEFAULT_SETTINGS: dict[str, Any] = {
    "selected_model": "medium",
    "microphone_device": "default",
    "shortcut": "Ctrl+Win",
    "shortcut_mode": "hold",
    "cancel_shortcut": "Esc",
    "insert_method": "clipboard_paste",
    "restore_clipboard": True,
    "auto_download_model": True,
    "auto_punctuation": False,
    "auto_capitalization": False,
    "add_space_after_text": False,
    "press_enter_after_text": False,
    "typing_delay_ms": 80,
    "start_with_windows": False,
    "minimize_to_tray": True,
    "show_overlay": False,
    "save_history": False,
    "model_path": default_model_path(),
    "use_gpu": False,
    "language_mode": "auto",
    "forced_language": "",
    "translate_to_english": False,
    "delete_temp_audio": True,
    "debug_logs": False,
}


class SettingsManager:
    """Loads and saves the readable V1 settings JSON file."""

    def __init__(self) -> None:
        self.config_dir = _appdata_root() / APP_NAME
        self.settings_path = self.config_dir / "settings.json"
        self._settings = deepcopy(DEFAULT_SETTINGS)
        self.load()

    @property
    def settings_path_text(self) -> str:
        return str(self.settings_path)

    def ensure_local_folders(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        Path(str(self.get("model_path"))).expanduser().mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not self.settings_path.exists():
            self.save()
            self.ensure_local_folders()
            return self.all()

        try:
            with self.settings_path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
        except (OSError, json.JSONDecodeError):
            loaded = {}

        merged = deepcopy(DEFAULT_SETTINGS)
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                if key in merged:
                    merged[key] = value

        self._settings = merged
        self.save()
        self.ensure_local_folders()
        return self.all()

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.settings_path.with_suffix(".json.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(self._settings, file, indent=2)
            file.write("\n")
        temp_path.replace(self.settings_path)

    def all(self) -> dict[str, Any]:
        return deepcopy(self._settings)

    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(f"Unknown setting: {key}")
        self._settings[key] = value
        if save:
            self.save()
            self.ensure_local_folders()

    def update(self, values: dict[str, Any], save: bool = True) -> None:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                raise KeyError(f"Unknown setting: {key}")
            self._settings[key] = value
        if save:
            self.save()
            self.ensure_local_folders()

    def reset(self) -> None:
        self._settings = deepcopy(DEFAULT_SETTINGS)
        self.save()
        self.ensure_local_folders()
