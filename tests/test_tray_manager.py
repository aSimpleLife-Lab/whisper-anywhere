from __future__ import annotations

import os
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # the tray icon is never shown

import pytest
from PySide6.QtWidgets import QApplication

from core import tray_manager
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


def test_a_problem_stays_on_the_tray_icon_until_the_next_state(tray) -> None:
    # A dictation that failed while the window sat in the tray gave only the
    # error sound: nothing said why.
    tray.show_problem("", "The app did not take the paste.")  # a plain error has no heading

    assert tray.tray.toolTip() == "Whisper Anywhere: The app did not take the paste."
    tray.set_state("error")  # the window's state follows: the reason stays
    assert tray.tray.toolTip() == "Whisper Anywhere: The app did not take the paste."
    tray.set_state("listening")
    assert tray.tray.toolTip() == "Whisper Anywhere: listening"
    tray.set_state("idle")
    assert tray.tray.toolTip() == "Whisper Anywhere"


def test_a_long_problem_fits_the_tooltip(tray) -> None:
    # Windows cuts a tray tooltip at 127 characters, mid-word.
    tray.show_problem("Not typed", "word " * 60)

    assert len(tray.tray.toolTip()) <= 127 and tray.tray.toolTip().endswith("…")


def test_a_problem_is_announced_once_when_nobody_is_looking(tray, monkeypatch) -> None:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(tray, "_can_announce", lambda: True)
    monkeypatch.setattr(tray.tray, "showMessage", lambda title, text, icon, ms: shown.append((title, text)))

    tray.show_problem("Not typed", "The shortcut seemed stuck.")
    tray.show_problem("Not typed", "The shortcut seemed stuck.")  # the same again, right after
    tray.show_problem("Hotkey error", "The global shortcut stopped working.")
    tray.show_problem("", "The app did not take the paste.")  # a plain error gets a heading here

    assert shown == [
        ("Not typed", "The shortcut seemed stuck."),
        ("Hotkey error", "The global shortcut stopped working."),
        ("Dictation problem", "The app did not take the paste."),
    ]


def test_no_announcement_over_a_game_or_while_the_window_is_in_use(tray, monkeypatch) -> None:
    shown: list[tuple] = []
    monkeypatch.setattr(tray, "_can_announce", lambda: False)
    monkeypatch.setattr(tray.tray, "showMessage", lambda *args: shown.append(args))

    tray.show_problem("", "No microphone was found.")

    assert shown == []
    assert tray.tray.toolTip() == "Whisper Anywhere: No microphone was found."  # still there to read later


def test_the_tray_menu_pauses_dictation_and_follows_the_window(tray) -> None:
    # Pausing the shortcut meant opening the window first.
    asked: list[bool] = []
    tray.dictation_toggled.connect(asked.append)

    tray.dictation_action.trigger()
    assert asked == [False]

    tray.set_dictation(False)  # the window's switch went off and said so
    assert not tray.dictation_action.isChecked()
    assert tray.tray.toolTip() == "Whisper Anywhere: dictation is off"
    tray.set_state("idle")  # a state change while paused keeps saying so
    assert tray.tray.toolTip() == "Whisper Anywhere: dictation is off"

    tray.set_dictation(True)
    assert tray.dictation_action.isChecked() and tray.tray.toolTip() == "Whisper Anywhere"


def test_a_problem_while_the_microphone_is_open_keeps_the_listening_dot(tray) -> None:
    # An update check that failed during a dictation turned the dot red while
    # the microphone was still open, and it stayed red until the next change.
    tray.set_state("listening")

    tray.show_problem("Update check failed", "The request timed out.")

    assert tray.tray.toolTip() == "Whisper Anywhere: listening"
    tray.set_state("error")  # the window's state once the dictation is over
    assert tray.tray.toolTip() == "Whisper Anywhere: Update check failed: The request timed out."


def test_announcements_need_a_visible_tray_no_game_and_no_window_in_use(tray, monkeypatch) -> None:
    visible, active, game = [True], [None], [False]
    monkeypatch.setattr(tray.tray, "isVisible", lambda: visible[0])
    monkeypatch.setattr(tray_manager.QApplication, "activeWindow", staticmethod(lambda: active[0]))
    monkeypatch.setattr(tray_manager, "foreground_is_fullscreen", lambda: game[0])

    assert tray._can_announce()
    game[0] = True
    assert not tray._can_announce()  # never over a game
    game[0], active[0] = False, object()
    assert not tray._can_announce()  # the app's own window says it
    active[0], visible[0] = None, False
    assert not tray._can_announce()  # no tray icon to announce from
