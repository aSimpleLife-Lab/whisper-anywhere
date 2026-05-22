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
    "settings_schema_version": 2,
    "first_run_setup_completed": False,
    "selected_model": "base",
    "microphone_device": "default",
    "shortcut": "Ctrl+Alt+Q",
    "shortcut_mode": "hold",
    "hotkey_default_version": 3,
    "cancel_shortcut": "Esc",
    "insert_method": "clipboard_paste",
    "restore_clipboard": True,
    "start_sound_path": "",
    "stop_sound_path": "",
    "device": "cpu",
    "compute_type": "int8",
    "performance_preset": "balanced",
    "runtime_default_version": 2,
    "use_gpu_if_available": False,
    "fallback_to_cpu": True,
    "low_ram_mode": False,
    "low_vram_mode": False,
    "warn_before_large_models": True,
    "cpu_threads": "auto",
    "auto_download_models": True,
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

    @property
    def hotkey_log_path(self) -> Path:
        return self.config_dir / "hotkey.log"

    @property
    def hotkey_log_path_text(self) -> str:
        return str(self.hotkey_log_path)

    def ensure_local_folders(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        Path(str(self.get("model_path"))).expanduser().mkdir(
            parents=True, exist_ok=True
        )

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

        if isinstance(loaded, dict):
            loaded = self._migrate_settings(loaded)
        else:
            loaded = {}

        merged = deepcopy(DEFAULT_SETTINGS)
        for key, value in loaded.items():
            if key in merged:
                merged[key] = value

        self._settings = self._normalize(merged)
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
        self._settings = self._normalize(self._settings)
        if save:
            self.save()
            self.ensure_local_folders()

    def update(self, values: dict[str, Any], save: bool = True) -> None:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                raise KeyError(f"Unknown setting: {key}")
            self._settings[key] = value
        self._settings = self._normalize(self._settings)
        if save:
            self.save()
            self.ensure_local_folders()

    def portable_export(self) -> dict[str, Any]:
        """Return settings that are safe to move to another PC."""
        exported = deepcopy(self._settings)
        default_path = default_model_path()
        for path_key in ("model_path", "start_sound_path", "stop_sound_path"):
            value = str(exported.get(path_key, "") or "")
            if not value:
                continue
            if path_key == "model_path" and value == default_path:
                continue
            exported[path_key] = ""
        exported["start_with_windows"] = False
        exported["settings_schema_version"] = DEFAULT_SETTINGS[
            "settings_schema_version"
        ]
        return exported

    def import_portable(self, values: dict[str, Any]) -> list[str]:
        if not isinstance(values, dict):
            raise ValueError("Settings import must be a JSON object.")
        accepted: dict[str, Any] = {}
        ignored: list[str] = []
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                ignored.append(str(key))
                continue
            if key in {"model_path", "start_sound_path", "stop_sound_path"}:
                candidate = str(value or "")
                if candidate and candidate != default_model_path():
                    ignored.append(key)
                    continue
            accepted[key] = value
        self.update(accepted)
        return ignored

    def reset(self) -> None:
        self._settings = deepcopy(DEFAULT_SETTINGS)
        self.save()
        self.ensure_local_folders()

    def _migrate_settings(self, loaded: dict[str, Any]) -> dict[str, Any]:
        migrated = dict(loaded)
        if "first_run_setup_completed" not in migrated:
            migrated["first_run_setup_completed"] = True
        shortcut_value = str(migrated.get("shortcut", "")).lower().replace(" ", "")
        hotkey_version = int(migrated.get("hotkey_default_version") or 0)
        if hotkey_version < 2 and shortcut_value == "ctrl+win":
            migrated["shortcut"] = "Ctrl+Win+Space"
            hotkey_version = 2
        if hotkey_version < 3 and shortcut_value in {
            "",
            "ctrl+win",
            "ctrl+win+space",
            "ctrl+alt+q",
        }:
            migrated["shortcut"] = "Ctrl+Alt+Q"
            hotkey_version = 3
        if hotkey_version:
            migrated["hotkey_default_version"] = hotkey_version
        runtime_version = int(migrated.get("runtime_default_version") or 0)
        if runtime_version < 2:
            if str(migrated.get("device", "auto")) == "auto" and bool(
                migrated.get("use_gpu_if_available", True)
            ):
                migrated["device"] = "cpu"
                migrated["compute_type"] = "int8"
                migrated["use_gpu_if_available"] = False
            migrated["runtime_default_version"] = 2
        if "auto_download_models" not in migrated and "auto_download_model" in migrated:
            migrated["auto_download_models"] = bool(migrated.get("auto_download_model"))
        if "device" not in migrated and "use_gpu" in migrated:
            migrated["device"] = "gpu" if bool(migrated.get("use_gpu")) else "auto"
        if "use_gpu_if_available" not in migrated and "use_gpu" in migrated:
            migrated["use_gpu_if_available"] = bool(migrated.get("use_gpu"))
        if (
            str(migrated.get("start_sound_path", "")).lower()
            == r"c:\users\ben\downloads\startsound.mp3"
        ):
            migrated["start_sound_path"] = ""
        if (
            str(migrated.get("stop_sound_path", "")).lower()
            == r"c:\users\ben\downloads\stopsound.mp3"
        ):
            migrated["stop_sound_path"] = ""
        return migrated

    def _normalize(self, values: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(values)
        if normalized.get("selected_model") not in {
            "tiny",
            "base",
            "small",
            "medium",
            "large",
            "large-v2",
            "large-v3",
            "turbo",
            "large-v3-turbo",
        }:
            normalized["selected_model"] = "base"
        if normalized.get("device") not in {"auto", "cpu", "gpu"}:
            normalized["device"] = "auto"
        if normalized.get("compute_type") not in {
            "auto",
            "int8",
            "int8_float16",
            "float16",
            "float32",
        }:
            normalized["compute_type"] = "auto"
        if normalized.get("performance_preset") not in {
            "fast",
            "balanced",
            "accurate",
            "low_ram",
            "low_vram",
        }:
            normalized["performance_preset"] = "balanced"
        if normalized.get("insert_method") != "clipboard_paste":
            normalized["insert_method"] = "clipboard_paste"
        cpu_threads = normalized.get("cpu_threads", "auto")
        if cpu_threads != "auto":
            try:
                normalized["cpu_threads"] = max(1, min(64, int(cpu_threads)))
            except (TypeError, ValueError):
                normalized["cpu_threads"] = "auto"
        return normalized
