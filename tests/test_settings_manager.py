from __future__ import annotations

import json

import pytest

import core.settings_manager as settings_module


@pytest.fixture
def isolated_settings_paths(tmp_path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    appdata_root = tmp_path / "AppData" / "Roaming"
    localappdata_root = tmp_path / "AppData" / "Local"
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
    assert manager.get("device") == "auto"
    assert manager.get("compute_type") == "auto"
    assert manager.get("insert_method") == "clipboard_paste"
    assert "unknown_setting" not in manager.all()


def test_settings_manager_rewrites_invalid_json_to_defaults(
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
    assert manager.get("low_ram_mode") is True


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
