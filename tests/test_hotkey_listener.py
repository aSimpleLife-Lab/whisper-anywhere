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


@pytest.fixture(autouse=True)
def modifiers_really_down(monkeypatch) -> None:
    # Tests press keys virtually; Windows' own key state would call them up.
    monkeypatch.setattr(hotkey_module, "_modifier_down", lambda token: True)


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
    listener.cancelled.connect(lambda: events.append("cancelled"))
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


def test_ctrl_letter_shortcuts_match() -> None:
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


def test_altgr_and_our_own_typing_are_not_shortcut_keys(monkeypatch) -> None:
    # Fed through pynput's real hook handling. Windows sends AltGr as a fake
    # left Ctrl (scan code 0x21D) plus Right Alt, so '@' on a German keyboard
    # (AltGr+Q) read as Ctrl+Alt+Q; our Unicode typing arrives as VK_PACKET.
    import ctypes

    from pynput import keyboard

    hooks: list = []
    monkeypatch.setattr(keyboard.Listener, "start", lambda self: hooks.append(self))
    listener = HotkeyListener("Ctrl+Alt+Q")
    events: list[str] = []
    listener.pressed.connect(lambda: events.append("pressed"))
    listener.start()
    hook = hooks[0]

    def send(msg: int, vk: int, scan: int) -> None:
        data = hook._KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan)
        converted = hook._convert(0, msg, ctypes.pointer(data))
        if converted is not None:
            hook._process(*converted)

    down, sysdown, up, sysup = 0x0100, 0x0104, 0x0101, 0x0105
    send(down, 0xA2, 0x21D)  # AltGr: fake left Ctrl...
    send(sysdown, 0xA5, 0x38)  # ...and Right Alt
    send(down, 0x51, 0x10)  # Q
    send(up, 0x51, 0x10)
    send(sysup, 0xA5, 0x38)
    send(up, 0xA2, 0x21D)
    send(down, hotkey_module.VK_PACKET, ord("q"))  # typed by us, not pressed
    assert events == []

    send(down, 0xA2, 0x1D)  # a real Ctrl+Alt+Q still fires
    send(sysdown, 0xA4, 0x38)
    send(sysdown, 0x51, 0x10)
    assert events == ["pressed"]
    listener.stop()


def test_modifiers_whose_key_up_was_missed_are_forgotten(monkeypatch) -> None:
    # Ctrl and Alt let go on the secure desktop (Ctrl+Alt+Del) never send a
    # key-up to our hook; a plain Q must not start dictation afterwards.
    listener, events, _mouse = listening("Ctrl+Alt+Q")
    listener._press_token("ctrl")
    listener._press_token("alt")
    monkeypatch.setattr(hotkey_module, "_modifier_down", lambda token: False)
    listener._press_token("q")
    assert events == []

    clicks, click_events, click_mouse = listening("Ctrl+M4")
    clicks._press_token("ctrl")
    clicks._mouse_filter(WM_XBUTTONDOWN, M4)  # Back still works
    assert click_events == [] and click_mouse.suppressed == 0


def test_toggle_presses_leave_start_or_stop_to_the_window() -> None:
    # The window knows whether it is recording; a refused start used to leave
    # the listener's own on/off flag inverted.
    listener, events, _mouse = listening("M4")
    listener.mode = "toggle"
    for _ in range(2):
        listener._mouse_filter(WM_XBUTTONDOWN, M4)
        listener._mouse_filter(WM_XBUTTONUP, M4)
    assert events == ["pressed", "pressed"]


def test_esc_asks_the_window_to_cancel() -> None:
    listener, events, _mouse = listening("Ctrl+Alt+Q")
    listener._on_press(next(k for k, t in hotkey_module.PYNPUT_KEY_TOKENS.items() if t == "esc"))
    assert events == ["cancelled"]


def test_side_button_holds_dictation_and_is_kept_from_other_apps() -> None:
    listener, events, mouse = listening("M4")

    listener._mouse_filter(WM_XBUTTONDOWN, M4)
    listener._mouse_filter(WM_XBUTTONUP, M4)

    assert events == ["pressed", "released"]
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
    assert events == ["pressed", "released"]
    assert mouse.suppressed == 2
    listener._cancel_watchdog_locked()
