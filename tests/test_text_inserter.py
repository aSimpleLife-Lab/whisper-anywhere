from __future__ import annotations

from contextlib import contextmanager

import core.text_inserter as text_module


class FakeClipboard:
    def __init__(self, initial_text: str) -> None:
        self.data = {13: initial_text}
        self.events: list[object] = []

    def OpenClipboard(self) -> None:
        self.events.append("open")

    def CloseClipboard(self) -> None:
        self.events.append("close")

    def IsClipboardFormatAvailable(self, fmt: int) -> bool:
        return fmt in self.data

    def GetClipboardData(self, fmt: int) -> str:
        self.events.append(("get", fmt))
        return str(self.data[fmt])

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
    fake_keyboard = FakeKeyboard()

    monkeypatch.setattr(text_module, "win32clipboard", fake_clipboard)
    monkeypatch.setattr(
        text_module, "win32con", type("FakeWin32Con", (), {"CF_UNICODETEXT": 13})()
    )
    monkeypatch.setattr(text_module, "Key", type("FakeKey", (), {"ctrl": "CTRL"})())
    monkeypatch.setattr(text_module.time, "sleep", lambda seconds: None)

    inserter = text_module.TextInserter()
    inserter._keyboard = fake_keyboard
    restore_calls: list[object] = []
    monkeypatch.setattr(
        inserter, "_restore_target", lambda target: restore_calls.append(target)
    )

    inserter.insert_text(
        "  café 你好  ",
        None,
        {
            "typing_delay_ms": 0,
            "restore_clipboard": True,
            "auto_capitalization": True,
            "auto_punctuation": True,
            "add_space_after_text": True,
            "press_enter_after_text": True,
        },
    )

    assert restore_calls == [None]
    assert fake_clipboard.data[13] == "keep me"
    assert [
        event[2]
        for event in fake_clipboard.events
        if isinstance(event, tuple) and event[0] == "set"
    ] == [
        "Café 你好. \n",
        "keep me",
    ]
    assert ("hold", "CTRL") in fake_keyboard.events
    assert ("press", "v") in fake_keyboard.events
    assert ("release", "v") in fake_keyboard.events


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
