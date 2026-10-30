from __future__ import annotations

import os

import main


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value) -> None:
        self[key] = value


def test_a_bad_saved_shortcut_falls_back_instead_of_crashing() -> None:
    # An imported 'Ctrl+F13' stopped the app at every sign-in.
    settings = FakeSettings(shortcut="Ctrl+F13")

    assert main.saved_shortcut(settings) == "Ctrl+Alt+Q"
    assert settings["shortcut"] == "Ctrl+Alt+Q"
    assert main.saved_shortcut(FakeSettings(shortcut="M3")) == "M3"


def test_only_one_copy_runs() -> None:
    # Two copies each recorded and pasted every dictation.
    name = f"WhisperAnywhereTest-{os.getpid()}"

    assert not main.another_copy_running(name)
    assert main.another_copy_running(name)
