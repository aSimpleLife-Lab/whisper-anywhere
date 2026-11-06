from __future__ import annotations

import os
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # the tray icon is never shown

import pytest
from PySide6.QtWidgets import QApplication

from core.tray_manager import TrayManager


@pytest.fixture
def tray() -> TrayManager:
    _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    models = [SimpleNamespace(name="voz", label="Voz"), SimpleNamespace(name="large-v3", label="Large v3")]
    return TrayManager(SimpleNamespace(selected_model=lambda: "voz", models=lambda: models))


def ticked(tray: TrayManager) -> list[str]:
    return [name for name, action in tray.model_actions.items() if action.isChecked()]


def test_a_refused_or_cancelled_tray_pick_does_not_stay_ticked(tray) -> None:
    picks: list[str] = []
    tray.model_selected.connect(picks.append)  # busy, or Cancel on the large-model warning

    tray.model_actions["large-v3"].trigger()
    tray.model_actions["voz"].trigger()  # the current model, picked again while busy

    assert picks == ["large-v3", "voz"]
    assert ticked(tray) == ["voz"]


def test_the_tick_follows_set_current_model(tray) -> None:
    tray.model_selected.connect(tray.set_current_model)  # the window switched and said so

    tray.model_actions["large-v3"].trigger()

    assert ticked(tray) == ["large-v3"]
