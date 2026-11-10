from __future__ import annotations

import json
import os
import tempfile
import time
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
    "settings_schema_version": 3,
    "first_run_setup_completed": False,
    "tutorial_seen": False,
    "tray_hint_shown": False,  # the first close to the tray says the app is still running
    "selected_model": "voz",
    "microphone_device": "default",
    "microphone_name": "",
    "shortcut": "Ctrl+Alt+Q",
    "shortcut_mode": "hold",
    "hotkey_default_version": 3,
    "retype_shortcut": "Ctrl+Alt+V",  # types the last dictation again; "" is off
    "insert_method": "auto",
    "restore_clipboard": True,
    "start_sound_path": "",
    "stop_sound_path": "",
    "feedback_volume": 0.5,
    "device": "cpu",
    "compute_type": "int8",
    "runtime_default_version": 2,
    "use_gpu_if_available": False,
    "fallback_to_cpu": True,
    "low_vram_mode": False,
    "warn_before_large_models": True,
    "cpu_threads": "auto",
    "unload_model_after": "never",  # idle minutes, or after "each" dictation
    "auto_download_models": True,
    "auto_punctuation": False,
    "auto_capitalization": False,
    "add_space_after_text": False,
    "press_enter_after_text": False,
    "app_profiles": {},
    "start_with_windows": False,
    "minimize_to_tray": True,
    "model_path": default_model_path(),
    "language_mode": "auto",
    "forced_language": "",
    "translate_to_english": False,
    "debug_logs": False,
}

# A value outside these (or of another type) goes back to its key's default.
CHOICES: dict[str, set[str]] = {
    "selected_model": {
        "voz",
        "Voz",
        "desert-ant-labs/voz",
        "tiny",
        "base",
        "small",
        "medium",
        "large",
        "large-v2",
        "large-v3",
        "turbo",
        "large-v3-turbo",
        "sensevoice-small",
        "SenseVoiceSmall",
        "iic/SenseVoiceSmall",
        "funasr-nano",
        "fun-asr-nano",
        "Fun-ASR-Nano",
        "Fun-ASR-Nano-2512",
        "FunAudioLLM/Fun-ASR-Nano-2512",
    },
    "shortcut_mode": {"hold", "toggle"},
    "insert_method": {"auto", "paste", "type"},
    "device": {"auto", "cpu", "gpu"},
    "compute_type": {"auto", "int8", "int8_float16", "float16", "float32"},
    "unload_model_after": {"never", "5", "15", "60", "each"},
}
# app_profiles maps a lower-case exe file name to these; a missing field takes
# the first choice.
PROFILE_CHOICES: dict[str, tuple[str, ...]] = {
    "insert_method": ("auto", "paste", "type"),
    "send_key": ("default", "enter", "ctrl+enter", "none"),
    "full_stop": ("default", "on", "off"),
}
MAX_APP_PROFILES = 50


def _parse_settings(raw: bytes) -> dict[str, Any] | None:
    try:
        try:
            loaded = json.loads(raw)  # also reads a BOM or UTF-16, as PowerShell 5.1 writes
        except UnicodeDecodeError:
            loaded = json.loads(raw.decode("mbcs"))  # a Notepad "ANSI" save
    except (ValueError, RecursionError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _read_settings(path: Path) -> dict[str, Any] | None:
    """The settings object in path, or None if it is missing or unreadable."""
    for attempt in range(3):
        try:
            return _parse_settings(path.read_bytes())
        except FileNotFoundError:
            return None
        except OSError:  # a backup or sync tool holding it for a moment
            if attempt < 2:
                time.sleep(0.2)
    return None


def _write_atomic(path: Path, data: bytes) -> None:
    """A unique temp file, forced to disk, then renamed over path: a power cut
    leaves the old file or the new one, never a zero-filled one."""
    handle, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _version(value: Any, current: int) -> int:
    """A *_default_version. Missing is 0 (never migrated); one that is not a
    number (a hand edit, an imported typo) counts as current and changes nothing."""
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return current


def _app_profiles(value: Any) -> dict[str, dict[str, str]]:
    """Per-app overrides by exe file name; a bad name or field value drops that entry."""
    profiles: dict[str, dict[str, str]] = {}
    if not isinstance(value, dict):
        return profiles
    for exe, profile in value.items():
        if not isinstance(exe, str) or not isinstance(profile, dict):
            continue
        exe = exe.lower()
        if not exe.endswith(".exe") or exe == ".exe" or any(char in exe for char in "\\/:"):
            continue
        fields = {field: profile.get(field, choices[0]) for field, choices in PROFILE_CHOICES.items()}
        if all(fields[field] in choices for field, choices in PROFILE_CHOICES.items()):
            profiles[exe] = fields
            if len(profiles) == MAX_APP_PROFILES:
                break
    return profiles


class SettingsManager:
    """Loads and saves the readable settings JSON file."""

    def __init__(self) -> None:
        self.config_dir = _appdata_root() / APP_NAME
        self.settings_path = self.config_dir / "settings.json"
        self.backup_path = self.config_dir / "settings.json.bak"
        self.load_notice = ""  # why the settings did not come from settings.json
        self.model_folder_error = ""  # set while the model folder is not available
        self._settings = deepcopy(DEFAULT_SETTINGS)
        self.load()

    @property
    def settings_path_text(self) -> str:
        return str(self.settings_path)

    @property
    def debug_log_path(self) -> Path:
        return self.config_dir / "debug.log"

    def ensure_local_folders(self) -> None:
        """Never raises for the model folder: on a drive that is not there right
        now (USB, NAS at sign-in) model_folder_error says so instead."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        folder = Path(str(self.get("model_path"))).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.model_folder_error = ""
        except OSError as exc:
            self.model_folder_error = (
                f"The model folder {folder} is not available ({exc.strerror or exc})."
            )

    def load(self) -> dict[str, Any]:
        """Never raises: an unreadable settings.json is kept as
        settings.json.broken, and the last good copy is used if there is one."""
        self.load_notice = ""
        existed = self.settings_path.exists()
        raw = _read_settings(self.settings_path)
        loaded = raw
        if raw is None and existed:
            kept = self._set_aside_unreadable_file()
            loaded = _read_settings(self.backup_path)
            source = "the last good copy" if loaded is not None else "the defaults"
            self.load_notice = f"settings.json could not be read, so {source} are in use." + (
                f" The unreadable file is kept as {kept.name}." if kept else ""
            )
        if loaded is None and not existed:
            self._settings = deepcopy(DEFAULT_SETTINGS)  # a first start
        else:
            self._settings = self._normalize(self._migrate_settings(loaded or {}))
        if self._settings != raw:
            try:
                self.save()
            except OSError as exc:
                self.load_notice = f"{self.load_notice} Settings could not be saved: {exc}".strip()
        try:
            self.ensure_local_folders()
        except OSError:
            pass  # no settings folder: the save above failed and said so
        return self.all()

    def _set_aside_unreadable_file(self) -> Path | None:
        """Keeps it for the user, never over an older one."""
        for number in range(1, 100):
            target = self.config_dir / ("settings.json.broken" + (f".{number}" if number > 1 else ""))
            if target.exists():
                continue
            try:
                self.settings_path.rename(target)
            except OSError:  # held by another program: it stays where it is
                return None
            return target
        return None

    def save(self) -> None:
        """The previous good file stays as settings.json.bak."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(self._settings, indent=2) + "\n").encode("utf-8")
        try:
            previous = self.settings_path.read_bytes()
        except OSError:
            previous = b""
        if previous == data:
            return
        if _parse_settings(previous) is not None:
            _write_atomic(self.backup_path, previous)
        _write_atomic(self.settings_path, data)

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
        """Settings that are safe to move to another PC or to share: no folder
        or file paths (they carry the Windows user name)."""
        exported = deepcopy(self._settings)
        for path_key in ("model_path", "start_sound_path", "stop_sound_path"):
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
            # Exports always say false and blank a custom model folder: both
            # stay this PC's own (its Run key, its models).
            if key == "start_with_windows" or (key == "model_path" and not value):
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
        hotkey_version = _version(migrated.get("hotkey_default_version"), 3)
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
        runtime_version = _version(migrated.get("runtime_default_version"), 2)
        if runtime_version < 2:
            if str(migrated.get("device", "auto")) == "auto" and bool(
                migrated.get("use_gpu_if_available", True)
            ):
                migrated["device"] = "cpu"
                migrated["compute_type"] = "int8"
                migrated["use_gpu_if_available"] = False
            migrated["runtime_default_version"] = 2
        if "unload_model_after" not in migrated:
            # Schema 3 merged Low RAM mode and "Keep the model loaded": either one
            # reloaded the model for every dictation, about 2 s each. Low RAM
            # also kept Whisper and FunASR on the CPU.
            low_ram = bool(migrated.get("low_ram_mode", False))
            unload = low_ram or not bool(migrated.get("keep_model_loaded", True))
            migrated["unload_model_after"] = "5" if unload else "never"
            if low_ram:
                migrated["device"] = "cpu"
                migrated["use_gpu_if_available"] = False
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
        """Each key on its own: a wrong type, or a value outside its choices,
        goes back to that key's default and leaves the others alone."""
        normalized: dict[str, Any] = {}
        for key, default in DEFAULT_SETTINGS.items():
            value = values.get(key, default)
            valid = type(value) is type(default) and (key not in CHOICES or value in CHOICES[key])
            normalized[key] = value if valid else deepcopy(default)
        normalized["settings_schema_version"] = DEFAULT_SETTINGS["settings_schema_version"]
        if normalized["device"] == "gpu" and not normalized["use_gpu_if_available"]:
            normalized["device"] = "cpu"  # v0.1's "Use GPU" box was off: it ran on the CPU
        if not normalized["model_path"]:
            normalized["model_path"] = default_model_path()  # "" meant the working folder
        normalized["app_profiles"] = _app_profiles(values.get("app_profiles"))
        cpu_threads = values.get("cpu_threads", "auto")
        if cpu_threads != "auto":
            try:
                normalized["cpu_threads"] = max(1, min(64, int(cpu_threads)))
            except (TypeError, ValueError, OverflowError):
                normalized["cpu_threads"] = "auto"
        try:
            normalized["feedback_volume"] = max(0.0, min(1.0, float(values.get("feedback_volume", 0.5))))
        except (TypeError, ValueError, OverflowError):
            normalized["feedback_volume"] = 0.5
        return normalized
