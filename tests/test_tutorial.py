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
