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

    assert manager.get("selected_model") == "base"
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


def test_settings_manager_rejects_unknown_keys(isolated_settings_paths: tuple) -> None:
    manager = settings_module.SettingsManager()

    with pytest.raises(KeyError, match="Unknown setting: not_real"):
        manager.set("not_real", "value")

    with pytest.raises(KeyError, match="Unknown setting: other_fake"):
        manager.update({"other_fake": True})
