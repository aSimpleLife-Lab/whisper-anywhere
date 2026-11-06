from __future__ import annotations

import json
import os
import string
from pathlib import Path

import pytest

import core.settings_manager as settings_module
from core.model_manager import ModelManager


@pytest.fixture
def isolated_settings_paths(tmp_path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    appdata_root = tmp_path / "AppData" / "Roaming"
    localappdata_root = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("APPDATA", str(appdata_root))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata_root))
    monkeypatch.setattr(settings_module, "_appdata_root", lambda: appdata_root)
    monkeypatch.setattr(
        settings_module, "_localappdata_root", lambda: localappdata_root
    )
    monkeypatch.setitem(
        settings_module.DEFAULT_SETTINGS,
        "model_path",
        str(localappdata_root / settings_module.APP_NAME / "models"),
    )
    return appdata_root, localappdata_root


def test_settings_manager_creates_defaults_and_serializes_json(
    isolated_settings_paths: tuple,
) -> None:
    _appdata_root, localappdata_root = isolated_settings_paths

    manager = settings_module.SettingsManager()

    saved = json.loads(manager.settings_path.read_text(encoding="utf-8"))
    assert saved["selected_model"] == settings_module.DEFAULT_SETTINGS["selected_model"]
    assert saved["shortcut"] == "Ctrl+Alt+Q"
    assert manager.settings_path.read_text(encoding="utf-8").endswith("\n")
    assert manager.get("model_path") == str(
        localappdata_root / settings_module.APP_NAME / "models"
    )


def test_settings_manager_falls_back_to_defaults_for_invalid_file_contents(
    isolated_settings_paths: tuple,
) -> None:
    appdata_root, _localappdata_root = isolated_settings_paths
    config_dir = appdata_root / settings_module.APP_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "selected_model": "not-a-model",
                "device": "bad-device",
                "compute_type": "bad-precision",
                "insert_method": "send_keys",
                "unknown_setting": "ignored",
            }
        ),
        encoding="utf-8",
    )

    manager = settings_module.SettingsManager()

    assert manager.get("selected_model") == "voz"
    assert manager.get("device") == "cpu"
    assert manager.get("compute_type") == "int8"
    assert manager.get("insert_method") == "auto"
    assert "unknown_setting" not in manager.all()


def test_an_unreadable_file_is_kept_and_the_defaults_are_used_without_a_backup(
    isolated_settings_paths: tuple,
) -> None:
    appdata_root, _localappdata_root = isolated_settings_paths
    config_dir = appdata_root / settings_module.APP_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text("{ not valid json", encoding="utf-8")

    manager = settings_module.SettingsManager()

    saved = json.loads(manager.settings_path.read_text(encoding="utf-8"))
    assert saved["shortcut"] == settings_module.DEFAULT_SETTINGS["shortcut"]
    assert (
        manager.get("selected_model")
        == settings_module.DEFAULT_SETTINGS["selected_model"]
    )
    assert (config_dir / "settings.json.broken").read_text(encoding="utf-8") == "{ not valid json"
    assert "settings.json.broken" in manager.load_notice


def write_good_settings_then_break_them(appdata_root, damage: bytes) -> None:
    manager = settings_module.SettingsManager()
    manager.update({"shortcut": "M3", "press_enter_after_text": True})
    manager.set("auto_punctuation", True)  # this save keeps the one before as .bak
    manager.settings_path.write_bytes(damage)


@pytest.mark.parametrize(
    "damage",
    [b"\x00" * 900, b'{"shortcut": "M3", "press_ent', b"", b"[1, 2]"],
    ids=["zero-filled after a power cut", "truncated", "empty", "not an object"],
)
def test_an_unreadable_file_falls_back_to_the_last_good_copy(
    isolated_settings_paths: tuple, damage: bytes
) -> None:
    # B8: any unreadable settings.json became the defaults with no copy kept:
    # M3 turned into Ctrl+Alt+Q and Enter after text went off.
    appdata_root, _localappdata_root = isolated_settings_paths
    write_good_settings_then_break_them(appdata_root, damage)

    manager = settings_module.SettingsManager()

    assert manager.get("shortcut") == "M3"
    assert manager.get("press_enter_after_text") is True
    assert (manager.config_dir / "settings.json.broken").read_bytes() == damage
    saved = json.loads(manager.settings_path.read_text(encoding="utf-8"))
    assert saved["shortcut"] == "M3"
    assert manager.load_notice


def test_an_older_broken_copy_is_never_overwritten(isolated_settings_paths: tuple) -> None:
    appdata_root, _localappdata_root = isolated_settings_paths
    write_good_settings_then_break_them(appdata_root, b"first damage")
    settings_module.SettingsManager()
    settings_module.SettingsManager().settings_path.write_bytes(b"second damage")

    manager = settings_module.SettingsManager()

    kept = sorted(p.read_bytes() for p in manager.config_dir.glob("settings.json.broken*"))
    assert kept == [b"first damage", b"second damage"]
    assert manager.get("shortcut") == "M3"


def test_a_save_reaches_the_disk_before_it_replaces_the_file(
    isolated_settings_paths: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B8: no fsync, so a power cut right after a save could leave settings.json
    # zero-filled; the fixed temp name could also collide.
    manager = settings_module.SettingsManager()
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os,
        "replace",
        lambda src, dst: (calls.append(f"replace {Path(src).name} -> {Path(dst).name}"), real_replace(src, dst))[1],
    )

    manager.set("shortcut_mode", "toggle")

    replaces = [call for call in calls if call.startswith("replace")]
    assert replaces and replaces[-1].endswith("-> settings.json")
    assert calls[calls.index(replaces[-1]) - 1] == "fsync"
    assert not replaces[-1].startswith("replace settings.json.tmp ")  # a unique temp name
    assert "settings.json.bak" in " ".join(replaces)
    backup = json.loads((manager.config_dir / "settings.json.bak").read_text(encoding="utf-8"))
    assert backup["shortcut_mode"] == "hold"  # the previous good file
    assert json.loads(manager.settings_path.read_text(encoding="utf-8"))["shortcut_mode"] == "toggle"
    assert not list(manager.config_dir.glob("*.tmp"))


def test_a_start_does_not_rewrite_a_file_that_needs_no_change(
    isolated_settings_paths: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_module.SettingsManager().set("shortcut", "M3")
    writes: list[str] = []
    monkeypatch.setattr(os, "replace", lambda src, dst: writes.append(str(dst)))

    settings_module.SettingsManager()

    assert writes == []


def test_a_bad_value_goes_back_to_its_own_default_only(isolated_settings_paths: tuple) -> None:
    # B8: a list or dict where a string belongs crashed every start, and any
    # unreadable value cost every other setting too.
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(
        appdata_root,
        json.dumps(
            {
                "selected_model": ["voz"],
                "device": {"gpu": True},
                "shortcut_mode": ["hold"],
                "unload_model_after": 5,
                "insert_method": {"auto": 1},
                "language_mode": None,
                "press_enter_after_text": "yes",
                "cpu_threads": [8],
                "feedback_volume": {"x": 1},
                "model_path": ["D:\\Models"],
                "shortcut": "M3",
                "auto_punctuation": True,
            }
        ).encode(),
    )

    manager = settings_module.SettingsManager()

    defaults = settings_module.DEFAULT_SETTINGS
    for key in (
        "selected_model", "device", "shortcut_mode", "unload_model_after", "insert_method",
        "language_mode", "press_enter_after_text", "cpu_threads", "feedback_volume", "model_path",
    ):
        assert manager.get(key) == defaults[key], key
    assert manager.get("shortcut") == "M3"
    assert manager.get("auto_punctuation") is True


def test_numbers_too_big_or_infinite_do_not_stop_the_start(isolated_settings_paths: tuple) -> None:
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(
        appdata_root,
        b'{"feedback_volume": 1' + b"0" * 400 + b', "cpu_threads": Infinity, '
        b'"hotkey_default_version": Infinity, "shortcut": "M3"}',
    )

    manager = settings_module.SettingsManager()

    assert manager.get("feedback_volume") == 0.5
    assert manager.get("cpu_threads") == "auto"
    assert manager.get("hotkey_default_version") == 3
    assert manager.get("shortcut") == "M3"


def test_set_and_update_never_keep_a_value_that_would_stop_the_next_start(
    isolated_settings_paths: tuple,
) -> None:
    manager = settings_module.SettingsManager()

    manager.import_portable({"selected_model": ["voz"], "shortcut_mode": "toggle"})
    manager.set("device", {"gpu": True})

    assert manager.get("selected_model") == "voz"
    assert manager.get("device") == "cpu"
    assert manager.get("shortcut_mode") == "toggle"
    assert settings_module.SettingsManager().get("shortcut_mode") == "toggle"


@pytest.mark.parametrize("bad", ["3.0", "v3", [3], {"v": 3}, "2b"])
def test_a_version_that_is_not_a_number_does_not_stop_the_start(
    isolated_settings_paths: tuple, bad
) -> None:
    # UB-1: an imported "2b" was saved, and the next sign-in died in _migrate_settings.
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(
        appdata_root,
        json.dumps(
            {"shortcut": "Ctrl+Win+Space", "hotkey_default_version": bad, "runtime_default_version": bad}
        ).encode(),
    )
    manager = settings_module.SettingsManager()
    manager.import_portable({"hotkey_default_version": bad, "runtime_default_version": bad})

    reloaded = settings_module.SettingsManager()

    assert reloaded.get("hotkey_default_version") == 3
    assert reloaded.get("runtime_default_version") == 2
    assert reloaded.get("shortcut") == "Ctrl+Win+Space"  # a bad version re-ran no old migration


def missing_drive_letter() -> str:
    present = {letter for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")}
    free = [letter for letter in "QWXYZ" if letter not in present]
    if not free:
        pytest.skip("no free drive letter to stand for a missing drive")
    return free[0]


def test_a_model_folder_on_a_missing_drive_does_not_stop_the_start(
    isolated_settings_paths: tuple,
) -> None:
    # A USB drive or NAS not there yet at sign-in stopped the app.
    appdata_root, _localappdata_root = isolated_settings_paths
    folder = f"{missing_drive_letter()}:\\Models\\Whisper"
    write_settings_file(appdata_root, json.dumps({"model_path": folder, "shortcut": "M3"}).encode())

    manager = settings_module.SettingsManager()
    manager.set("auto_punctuation", True)

    assert manager.get("model_path") == folder
    assert folder in manager.model_folder_error
    model_manager = ModelManager(manager)
    assert model_manager.model_storage_path() == Path(folder)
    assert model_manager.is_model_ready("voz") is False
    assert model_manager.model_status_label("small") == "Download"


def test_schema_3_turns_low_ram_and_keep_loaded_into_unload_after(
    isolated_settings_paths: tuple,
) -> None:
    appdata_root, _localappdata_root = isolated_settings_paths
    old = {
        "settings_schema_version": 2,
        "first_run_setup_completed": True,
        "shortcut": "M3",
        "cancel_shortcut": "Esc",
        "insert_method": "clipboard_paste",
        "device": "auto",
        "use_gpu_if_available": True,
        "compute_type": "int8",
        "runtime_default_version": 2,
        "low_ram_mode": True,
        "keep_model_loaded": True,
        "performance_preset": "balanced",
        "press_enter_after_text": True,
        "typing_delay_ms": 80,
        "show_overlay": False,
        "save_history": False,
        "delete_temp_audio": True,
        "start_with_windows": True,
        "microphone_device": "1",
        "microphone_name": "Microphone (Yeti Classic)",
    }
    write_settings_file(appdata_root, json.dumps(old).encode())

    manager = settings_module.SettingsManager()

    saved = json.loads(manager.settings_path.read_text(encoding="utf-8"))
    assert saved["settings_schema_version"] == 3
    # Low RAM unloaded after every dictation (~2 s each); now after 5 idle minutes.
    assert saved["unload_model_after"] == "5"
    assert (saved["device"], saved["use_gpu_if_available"]) == ("cpu", False)  # it stayed on the CPU
    assert saved["insert_method"] == "auto"
    assert saved["retype_shortcut"] == "Ctrl+Alt+V"
    assert saved["app_profiles"] == {}
    for gone in (
        "keep_model_loaded", "low_ram_mode", "show_overlay", "save_history",
        "delete_temp_audio", "cancel_shortcut", "performance_preset", "typing_delay_ms",
    ):
        assert gone not in saved
    for key in ("shortcut", "press_enter_after_text", "start_with_windows", "microphone_device", "microphone_name"):
        assert saved[key] == old[key]


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        ({"keep_model_loaded": False}, "5"),
        ({"keep_model_loaded": True, "low_ram_mode": False}, "never"),
        ({}, "never"),
        ({"unload_model_after": "15", "low_ram_mode": True}, "15"),
    ],
)
def test_unload_after_follows_the_old_switches_once(
    isolated_settings_paths: tuple, old: dict, expected: str
) -> None:
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(appdata_root, json.dumps(old).encode())

    assert settings_module.SettingsManager().get("unload_model_after") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("clipboard_paste", "auto"), ("send_keys", "auto"), ("paste", "paste"), ("type", "type"), ("auto", "auto")],
)
def test_insert_method_is_auto_paste_or_type(
    isolated_settings_paths: tuple, value: str, expected: str
) -> None:
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(appdata_root, json.dumps({"insert_method": value}).encode())

    assert settings_module.SettingsManager().get("insert_method") == expected


def test_app_profiles_keep_only_valid_entries(isolated_settings_paths: tuple) -> None:
    manager = settings_module.SettingsManager()
    profiles = {
        "Discord.exe": {"send_key": "enter"},
        "WindowsTerminal.exe": {"insert_method": "paste", "send_key": "none", "full_stop": "off"},
        "game.exe": {"insert_method": "type", "full_stop": "off", "typing_delay_ms": 5},
        "C:\\Games\\game.exe": {"send_key": "none"},
        "tools/app.exe": {},
        "notepad": {},
        ".exe": {},
        "slack.exe": {"send_key": "shift+enter"},
        "teams.exe": "paste",
        "code.exe": {"insert_method": ["paste"]},
    }
    profiles.update({f"app{number}.exe": {} for number in range(60)})

    manager.set("app_profiles", profiles)

    saved = manager.get("app_profiles")
    assert saved["discord.exe"] == {"insert_method": "auto", "send_key": "enter", "full_stop": "default"}
    assert saved["windowsterminal.exe"] == {"insert_method": "paste", "send_key": "none", "full_stop": "off"}
    assert saved["game.exe"] == {"insert_method": "type", "send_key": "default", "full_stop": "off"}
    for dropped in ("c:\\games\\game.exe", "tools/app.exe", "notepad", ".exe", "slack.exe", "teams.exe", "code.exe"):
        assert dropped not in saved
    assert len(saved) == 50
    assert list(saved)[:3] == ["discord.exe", "windowsterminal.exe", "game.exe"]
    assert settings_module.SettingsManager().get("app_profiles") == saved


def test_the_retype_shortcut_defaults_to_ctrl_alt_v_and_blank_is_off(
    isolated_settings_paths: tuple,
) -> None:
    manager = settings_module.SettingsManager()
    assert manager.get("retype_shortcut") == "Ctrl+Alt+V"

    manager.set("retype_shortcut", "")

    assert settings_module.SettingsManager().get("retype_shortcut") == ""


def test_an_export_carries_no_user_path(isolated_settings_paths: tuple, tmp_path) -> None:
    # B22: the default model folder went out as C:\Users\<name>\AppData\...
    manager = settings_module.SettingsManager()
    manager.update({"start_sound_path": str(tmp_path / "start.wav"), "shortcut": "M3"})

    exported = manager.portable_export()

    assert exported["model_path"] == ""
    assert exported["start_sound_path"] == ""
    assert not [value for value in exported.values() if str(tmp_path) in str(value)]
    assert exported["shortcut"] == "M3"
    manager.set("model_path", str(tmp_path / "custom models"))
    assert manager.portable_export()["model_path"] == ""


def test_settings_manager_persists_set_and_update_changes(
    isolated_settings_paths: tuple,
) -> None:
    manager = settings_module.SettingsManager()

    manager.set("selected_model", "small")
    manager.update({"auto_punctuation": True, "cpu_threads": "8"})

    reloaded = settings_module.SettingsManager()
    saved = json.loads(reloaded.settings_path.read_text(encoding="utf-8"))

    assert reloaded.get("selected_model") == "small"
    assert reloaded.get("auto_punctuation") is True
    assert reloaded.get("cpu_threads") == 8
    assert saved["selected_model"] == "small"
    assert saved["auto_punctuation"] is True
    assert saved["cpu_threads"] == 8


def test_feedback_volume_defaults_to_half_and_stays_in_range(
    isolated_settings_paths: tuple,
) -> None:
    manager = settings_module.SettingsManager()
    assert manager.get("feedback_volume") == 0.5

    manager.set("feedback_volume", 1.7)
    assert manager.get("feedback_volume") == 1.0
    manager.set("feedback_volume", "loud")
    assert manager.get("feedback_volume") == 0.5


def test_settings_manager_rejects_unknown_keys(isolated_settings_paths: tuple) -> None:
    manager = settings_module.SettingsManager()

    with pytest.raises(KeyError, match="Unknown setting: not_real"):
        manager.set("not_real", "value")

    with pytest.raises(KeyError, match="Unknown setting: other_fake"):
        manager.update({"other_fake": True})


def write_settings_file(appdata_root, content: bytes) -> None:
    config_dir = appdata_root / settings_module.APP_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_bytes(content)


def test_a_settings_file_saved_with_a_bom_keeps_every_value(
    isolated_settings_paths: tuple,
) -> None:
    # PowerShell 5.1's Set-Content -Encoding UTF8 writes a BOM, and every
    # setting quietly went back to its default.
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(
        appdata_root,
        json.dumps({"shortcut": "Ctrl+Alt+Space", "low_ram_mode": True}).encode("utf-8-sig"),
    )

    manager = settings_module.SettingsManager()

    assert manager.get("shortcut") == "Ctrl+Alt+Space"
    assert manager.get("unload_model_after") == "5"


def test_an_ansi_settings_file_loads_instead_of_stopping_the_app(
    isolated_settings_paths: tuple,
) -> None:
    # Notepad's "ANSI" save: the é is not UTF-8, and the app died at start.
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(
        appdata_root,
        json.dumps(
            {"shortcut_mode": "toggle", "start_sound_path": "début.wav"}, ensure_ascii=False
        ).encode("cp1252"),
    )

    manager = settings_module.SettingsManager()

    assert manager.get("shortcut_mode") == "toggle"


def test_import_leaves_start_with_windows_to_this_pc(isolated_settings_paths: tuple) -> None:
    # Exports always say false, so importing your own file showed the switch
    # off while the Run key kept starting the app at sign-in.
    manager = settings_module.SettingsManager()
    manager.set("start_with_windows", True)

    manager.import_portable({"start_with_windows": False, "shortcut_mode": "toggle"})

    assert manager.get("start_with_windows") is True
    assert manager.get("shortcut_mode") == "toggle"


def test_an_import_without_a_model_folder_keeps_this_pcs_folder(
    isolated_settings_paths: tuple, tmp_path
) -> None:
    # Exports blank a custom model folder; "" then meant the working folder,
    # where the next shortcut press downloaded Voz.
    manager = settings_module.SettingsManager()
    custom = str(tmp_path / "my models")
    manager.set("model_path", custom)

    ignored = manager.import_portable({"model_path": ""})

    assert manager.get("model_path") == custom
    assert ignored == []


def test_an_empty_model_folder_means_the_default_one(isolated_settings_paths: tuple) -> None:
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(appdata_root, json.dumps({"model_path": ""}).encode())

    manager = settings_module.SettingsManager()

    assert manager.get("model_path") == settings_module.default_model_path()


def test_gpu_with_gpu_use_switched_off_is_shown_as_cpu(isolated_settings_paths: tuple) -> None:
    # v0.1 had both a GPU choice and a "Use GPU" box; with the box off the
    # engine ran on the CPU while the window showed GPU.
    appdata_root, _localappdata_root = isolated_settings_paths
    write_settings_file(
        appdata_root,
        json.dumps(
            {"device": "gpu", "use_gpu_if_available": False, "runtime_default_version": 2}
        ).encode(),
    )

    manager = settings_module.SettingsManager()

    assert manager.get("device") == "cpu"
