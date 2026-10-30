from __future__ import annotations

from types import SimpleNamespace

import pytest
from pynput.keyboard import KeyCode

import core.hotkey_listener as hotkey_module
from core.hotkey_listener import (
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    HotkeyListener,
    parse_shortcut,
    shortcut_warning,
)

M4 = SimpleNamespace(mouseData=1 << 16)  # XBUTTON1 in the hook's mouseData
M5 = SimpleNamespace(mouseData=2 << 16)


class FakeMouseListener:
    def __init__(self) -> None:
        self.suppressed = 0

    def suppress_event(self) -> None:
        self.suppressed += 1


def listening(shortcut: str) -> tuple[HotkeyListener, list[str], FakeMouseListener]:
    listener = HotkeyListener(shortcut)
    listener._running = True
    listener._mouse_listener = mouse = FakeMouseListener()
    events: list[str] = []
    listener.pressed.connect(lambda: events.append("pressed"))
    listener.released.connect(lambda hwnd: events.append("released"))
    listener.feedback.connect(lambda active: events.append("sound-on" if active else "sound-off"))
    return listener, events, mouse


def test_model_loads_unhook_the_keyboard_and_mouse(monkeypatch) -> None:
    # A model load holds Python's GIL for seconds. With our hooks in, Windows
    # held back every click and key press on the PC until it finished.
    hooks: list = []

    class FakeHook:
        def __init__(self, **_callbacks) -> None:
            self.live = False

        def start(self) -> None:
            self.live = True
            hooks.append(self)

        def stop(self) -> None:
            self.live = False

        def join(self, timeout=None) -> None:
            pass

        def is_alive(self) -> bool:
            return self.live

    def live() -> int:
        return sum(hook.live for hook in hooks)

    monkeypatch.setattr(hotkey_module, "pynput_keyboard", SimpleNamespace(Listener=FakeHook))
    monkeypatch.setattr(hotkey_module, "pynput_mouse", SimpleNamespace(Listener=FakeHook))
    listener = HotkeyListener("M3")
    listener.start()
    assert live() == 2  # keyboard and mouse
    with listener.paused():
        assert live() == 0
    assert live() == 2
    released: list[object] = []
    listener.released.connect(released.append)
    listener._press_token("mouse3")  # held as a load starts, let go during it
    with listener.paused():
        pass
    assert len(released) == 1  # the recording still stops
    with listener.paused():
        listener.stop()  # Dictation switched off during a load stays off
    assert live() == 0


def test_mouse_buttons_parse_as_shortcut_triggers() -> None:
    assert parse_shortcut("M4").mouse_trigger == "mouse4"
    assert parse_shortcut("m5").display == "M5"
    assert parse_shortcut("Ctrl+M1").display == "Ctrl+M1"
    assert parse_shortcut("Ctrl+Alt+Q").mouse_trigger is None
    with pytest.raises(ValueError, match="ordinary click"):
        parse_shortcut("M1")
    assert shortcut_warning("M4") == ""
    assert "no longer reaches apps" in shortcut_warning("Ctrl+M2")


def test_ctrl_letter_shortcuts_match_and_typed_characters_are_ignored() -> None:
    listener, events, _mouse = listening("Ctrl+Shift+D")
    ctrl, shift = next(k for k, t in hotkey_module.PYNPUT_KEY_TOKENS.items() if t == "ctrl"), next(
        k for k, t in hotkey_module.PYNPUT_KEY_TOKENS.items() if t == "shift"
    )
    listener._on_press(ctrl)
    listener._on_press(shift)
    # With Ctrl held, pynput reports the control character '\x04' instead of 'd'.
    listener._on_press(KeyCode(vk=0x44, char="\x04"))
    assert events[-1] == "pressed"
    listener._cancel_watchdog_locked()

    single, single_events, _ = listening("Q")
    single._on_press(KeyCode(vk=hotkey_module.VK_PACKET, char="q"))  # our own Unicode typing
    assert single_events == []


def test_side_button_holds_dictation_and_is_kept_from_other_apps() -> None:
    listener, events, mouse = listening("M4")

    listener._mouse_filter(WM_XBUTTONDOWN, M4)
    listener._mouse_filter(WM_XBUTTONUP, M4)

    assert events == ["sound-on", "pressed", "sound-off", "released"]
    assert mouse.suppressed == 2  # neither click reaches the browser's Back
    listener._cancel_watchdog_locked()


def test_other_buttons_and_missing_modifiers_pass_through() -> None:
    listener, events, mouse = listening("Ctrl+M4")

    listener._mouse_filter(WM_XBUTTONDOWN, M5)
    listener._mouse_filter(WM_XBUTTONDOWN, M4)  # Ctrl is not held
    listener._mouse_filter(WM_XBUTTONUP, M4)
    assert events == [] and mouse.suppressed == 0

    listener._press_token("ctrl")
    listener._mouse_filter(WM_XBUTTONDOWN, M4)
    listener._mouse_filter(WM_XBUTTONUP, M4)
    assert events == ["sound-on", "pressed", "sound-off", "released"]
    assert mouse.suppressed == 2
    listener._cancel_watchdog_locked()
