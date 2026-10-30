from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

import core.text_inserter as text_module


class FakeClipboard:
    def __init__(self, initial_text: str) -> None:
        self.data = {13: initial_text}
        self.events: list[object] = []

    def OpenClipboard(self) -> None:
        self.events.append("open")

    def CloseClipboard(self) -> None:
        self.events.append("close")

    def EnumClipboardFormats(self, fmt: int) -> int:
        later = sorted(f for f in self.data if f > fmt)
        return later[0] if later else 0

    def IsClipboardFormatAvailable(self, fmt: int) -> bool:
        return fmt in self.data

    def GetClipboardData(self, fmt: int) -> object:
        self.events.append(("get", fmt))
        return self.data[fmt]

    def EmptyClipboard(self) -> None:
        self.events.append("empty")
        self.data.clear()

    def SetClipboardData(self, fmt: int, value: str) -> None:
        self.events.append(("set", fmt, value))
        self.data[fmt] = value


class FakeKeyboard:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    @contextmanager
    def pressed(self, key: str):
        self.events.append(("hold", key))
        yield
        self.events.append(("release-hold", key))

    def press(self, key: str) -> None:
        self.events.append(("press", key))

    def release(self, key: str) -> None:
        self.events.append(("release", key))


def test_insert_text_preserves_clipboard_and_pastes_unicode(monkeypatch) -> None:
    fake_clipboard = FakeClipboard("keep me")
    fake_clipboard.data[8] = b"image bits"  # a copied screenshot (CF_DIB) must survive too
    fake_keyboard = FakeKeyboard()

    monkeypatch.setattr(text_module, "win32clipboard", fake_clipboard)
    monkeypatch.setattr(text_module, "CLIPBOARD_RESTORE_DELAY", 0)
    monkeypatch.setattr(
        text_module, "win32con", type("FakeWin32Con", (), {"CF_UNICODETEXT": 13})()
    )
    monkeypatch.setattr(
        text_module, "Key", type("FakeKey", (), {"ctrl": "CTRL", "enter": "ENTER"})()
    )
    monkeypatch.setattr(text_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(text_module, "foreground_is_fullscreen", lambda: False)

    inserter = text_module.TextInserter()
    inserter._keyboard = fake_keyboard

    inserter.insert_text(
        "  café 你好  ",
        None,
        {
            "restore_clipboard": True,
            "auto_capitalization": True,
            "auto_punctuation": True,
            "add_space_after_text": True,
            "press_enter_after_text": True,
        },
    )

    for thread in threading.enumerate():  # the old clipboard comes back on a timer
        if isinstance(thread, threading.Timer):
            thread.join()
    assert fake_clipboard.data[13] == "keep me"
    assert fake_clipboard.data[8] == b"image bits"
    assert [
        event[2]
        for event in fake_clipboard.events
        if isinstance(event, tuple) and event[0] == "set" and event[1] == 13
    ] == [
        "Café 你好. ",
        "keep me",
    ]
    assert ("hold", "CTRL") in fake_keyboard.events
    assert ("press", "v") in fake_keyboard.events
    assert ("release", "v") in fake_keyboard.events
    # Enter is a real key press after the paste, not a pasted newline.
    assert fake_keyboard.events[-2:] == [("press", "ENTER"), ("release", "ENTER")]


def test_full_screen_games_get_typed_text_and_keep_the_clipboard(monkeypatch) -> None:
    fake_clipboard = FakeClipboard("keep me")
    monkeypatch.setattr(text_module, "win32clipboard", fake_clipboard)
    monkeypatch.setattr(text_module, "foreground_is_fullscreen", lambda: True)
    monkeypatch.setattr(text_module.time, "sleep", lambda seconds: None)

    inserter = text_module.TextInserter()
    typed: list[int] = []
    monkeypatch.setattr(inserter, "_send_unicode_unit", typed.append)
    monkeypatch.setattr(inserter, "_wait_for_modifiers_released", lambda: None)

    inserter.insert_text("gg 👍", None, {})

    # The emoji is a surrogate pair: two UTF-16 units, each its own key press.
    assert typed == [ord("g"), ord("g"), ord(" "), 0xD83D, 0xDC4D]
    assert fake_clipboard.events == []


def test_text_is_not_typed_into_a_window_the_user_switched_to(monkeypatch) -> None:
    # Switching to another chat or a game while the clip is transcribed: the
    # words went there (and Enter sent them), or the old window covered the game.
    fake_clipboard = FakeClipboard("keep me")
    monkeypatch.setattr(text_module, "win32clipboard", fake_clipboard)
    monkeypatch.setattr(text_module.user32, "GetForegroundWindow", lambda: 222)
    inserter = text_module.TextInserter()
    monkeypatch.setattr(inserter, "_send_unicode_unit", lambda unit: pytest.fail("typed"))
    monkeypatch.setattr(inserter, "_send_ctrl_v", lambda: pytest.fail("pasted"))

    with pytest.raises(text_module.TextInsertionError, match="switched"):
        inserter.insert_text("secret", text_module.TextTarget(111), {"press_enter_after_text": True})

    assert fake_clipboard.events == []


def test_input_struct_matches_windows_size() -> None:
    # SendInput rejects every event unless cbSize is the real sizeof(INPUT).
    import ctypes

    assert ctypes.sizeof(text_module.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_pasted_text_is_marked_private_for_clipboard_history(monkeypatch) -> None:
    fake_clipboard = FakeClipboard("")
    registered: dict[str, int] = {}

    def register(name: str) -> int:
        return registered.setdefault(name, 49000 + len(registered))

    fake_clipboard.RegisterClipboardFormat = register
    monkeypatch.setattr(text_module, "win32clipboard", fake_clipboard)
    monkeypatch.setattr(
        text_module, "win32con", type("FakeWin32Con", (), {"CF_UNICODETEXT": 13})()
    )

    text_module.TextInserter().set_clipboard_text("secret words", private=True)

    assert set(registered) == set(text_module.PRIVATE_CLIPBOARD_FORMATS)
    assert fake_clipboard.data[registered["CanIncludeInClipboardHistory"]] == b"\0\0\0\0"
    assert fake_clipboard.data[13] == "secret words"


def test_prepare_text_formats_whitespace_and_empty_input() -> None:
    inserter = text_module.TextInserter()

    assert inserter._prepare_text("   ", {}) == ""
    assert (
        inserter._prepare_text(
            "  hello   there ",
            {
                "auto_capitalization": True,
                "auto_punctuation": True,
                "add_space_after_text": True,
                "press_enter_after_text": False,
            },
        )
        == "Hello there. "
    )
