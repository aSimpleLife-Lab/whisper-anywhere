from __future__ import annotations

from types import SimpleNamespace

import ui.main_window as mw


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value) -> None:
        self[key] = value


def test_tutorial_shows_once_and_replays_from_the_button(monkeypatch) -> None:
    shown: list[object] = []

    class FakeDialog:
        def __init__(self, parent) -> None:
            shown.append(parent)

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(mw, "TutorialDialog", FakeDialog)
    window = SimpleNamespace(settings_manager=FakeSettings())

    mw.MainWindow.show_tutorial(window)  # first time: shown
    mw.MainWindow.show_tutorial(window)  # already seen: not again on its own
    mw.MainWindow.show_tutorial(window, force=True)  # the ! button replays it

    assert len(shown) == 2
    assert window.settings_manager["tutorial_seen"] is True


def test_the_first_step_names_the_users_own_shortcut() -> None:
    assert "Hold M5" in mw._tutorial_pages("M5")[0][1]


def test_the_tour_says_where_problems_show_and_that_the_tray_can_pause() -> None:
    watching = mw._tutorial_pages("M3")[1][1]

    assert "red dot" in watching and "tray menu" in watching


def test_the_typing_step_names_the_users_retype_shortcut_or_none() -> None:
    assert "Ctrl+Shift+F9 types it again" in mw._tutorial_pages("M3", "Ctrl+Shift+F9")[3][1]
    assert "types it again" not in mw._tutorial_pages("M3", "")[3][1]
