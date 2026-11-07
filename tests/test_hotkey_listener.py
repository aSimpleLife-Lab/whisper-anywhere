from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from pynput.keyboard import KeyCode

import core.hotkey_listener as hotkey_module
from core.hotkey_listener import (
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    HotkeyListener,
    parse_shortcut,
    shortcut_problem,
    shortcut_warning,
)

M4 = SimpleNamespace(mouseData=1 << 16)  # XBUTTON1 in the hook's mouseData
M5 = SimpleNamespace(mouseData=2 << 16)
PLAIN = SimpleNamespace(mouseData=0)  # a real button or move: no marker
WM_MOUSEMOVE, WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0200, 0x0207, 0x0208


@pytest.fixture(autouse=True)
def modifiers_really_down(monkeypatch) -> None:
    # Tests press keys virtually; Windows' own key state would call them up.
    monkeypatch.setattr(hotkey_module, "_modifier_down", lambda token: True)


class FakeSendInput:
    """Stands in for SendInput: records each replayed click, sends nothing."""

    def __init__(self) -> None:
        self.clicks: list[list[tuple[int, int, int]]] = []
        self.sent = threading.Event()

    def __call__(self, count, inputs, size) -> int:
        mice = [inputs[index].value.mi for index in range(count)]
        self.clicks.append([(mi.dwFlags, mi.mouseData, mi.dwExtraInfo) for mi in mice])
        self.sent.set()
        return count


class InertTimer:
    """Stands in for the health check's QTimer: it never fires by itself."""

    def __init__(self, parent=None) -> None:
        self.timeout = SimpleNamespace(connect=lambda slot: None)
        self.active = False

    def setInterval(self, msec: int) -> None:
        pass

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False


@pytest.fixture(autouse=True)
def send_input(monkeypatch) -> FakeSendInput:
    # No click is ever sent, no health check runs by itself and Windows'
    # last-input time is never read: tests drive all three by hand.
    fake = FakeSendInput()
    monkeypatch.setattr(hotkey_module, "SendInput", fake, raising=False)
    monkeypatch.setattr(hotkey_module, "QTimer", InertTimer, raising=False)
    monkeypatch.setattr(hotkey_module, "_input_idle_seconds", lambda: None, raising=False)
    return fake


class Clock:
    """Stands in for the listener's time.monotonic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(hotkey_module, "time", clock)
    return clock


@pytest.fixture
def watchdogs(monkeypatch) -> list:
    """threading.Timer replaced: each watchdog lands here and is fired by hand."""
    armed: list = []

    class Watchdog:
        def __init__(self, seconds, function, args=()) -> None:
            self.seconds = seconds
            self.fire = lambda: function(*args)

        def start(self) -> None:
            armed.append(self)

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(hotkey_module.threading, "Timer", Watchdog)
    return armed


class FakeHook:
    """Stands in for a pynput listener: no thread, no Windows hook."""

    def __init__(self) -> None:
        self.live = False
        self.failure: Exception | None = None  # what pynput's join() re-raises
        self.suppressed = 0

    def start(self) -> None:
        self.live = True

    def stop(self) -> None:
        pass  # its thread winds down; join() waits for that

    def join(self, timeout=None) -> None:
        self.live = False
        if self.failure is not None:
            raise self.failure

    def is_alive(self) -> bool:
        return self.live

    def suppress_event(self) -> None:
        self.suppressed += 1


@pytest.fixture
def hooks(monkeypatch) -> list[FakeHook]:
    """pynput's listeners replaced by FakeHooks, in the order made."""
    made: list[FakeHook] = []

    def make(**callbacks) -> FakeHook:
        made.append(FakeHook())
        return made[-1]

    monkeypatch.setattr(hotkey_module, "pynput_keyboard", SimpleNamespace(Listener=make))
    monkeypatch.setattr(hotkey_module, "pynput_mouse", SimpleNamespace(Listener=make))
    return made


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
    listener.timed_out.connect(lambda: events.append("timed_out"))
    listener.tapped.connect(lambda: events.append("tapped"))
    return listener, events, mouse


def keyboard_hook(monkeypatch, shortcut: str):
    """A real pynput keyboard listener, never started, fed events the way its
    Windows hook would. send() says whether the event reached apps."""
    import ctypes

    from pynput import keyboard, mouse
    from pynput._util.win32 import SystemHook

    made: list = []
    monkeypatch.setattr(keyboard.Listener, "start", lambda self: made.append(self))
    monkeypatch.setattr(mouse.Listener, "start", lambda self: made.append(self))
    listener = HotkeyListener(shortcut)
    listener.start()
    hook = made[0]

    def send(msg: int, vk: int, scan: int = 0, flags: int = 0, extra: int = 0) -> str:
        data = hook._KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan, flags=flags, dwExtraInfo=extra)
        try:
            converted = hook._convert(0, msg, ctypes.pointer(data))
        except SystemHook.SuppressException:
            return "withheld"
        if converted is not None:
            hook._process(*converted)
        return "passed"

    return listener, send


def test_mouse_buttons_parse_as_shortcut_triggers() -> None:
    assert parse_shortcut("M4").mouse_trigger == "mouse4"
    assert parse_shortcut("m5").display == "M5"
    assert parse_shortcut("Ctrl+M1").display == "Ctrl+M1"
    assert parse_shortcut("Ctrl+Alt+Q").mouse_trigger is None
    with pytest.raises(ValueError, match="ordinary click"):
        parse_shortcut("M1")
    assert shortcut_warning("M4") == ""
    assert "no longer reaches apps" in shortcut_warning("Ctrl+M2")


def test_shortcuts_that_cancel_themselves_or_fire_while_typing_are_refused() -> None:
    # B18: Ctrl+Alt+Esc saved with no warning, and Esc cancels, so it
    # cancelled itself. B15: setup could save a bare Enter.
    for shortcut in ("Ctrl+Alt+Esc", "Esc", "Shift+Escape"):
        assert "Esc" in shortcut_problem(shortcut)
    for shortcut in ("Enter", "Tab", "Backspace", "Space", "Q", "7"):
        assert "while you type" in shortcut_problem(shortcut)
    for shortcut in ("Ctrl+Alt+Q", "M3", "F9", "Ctrl+Enter", "Shift+Q", "Backtick"):
        assert shortcut_problem(shortcut) == ""
    assert shortcut_problem("Ctrl+Q+W")  # can't be read either
    assert shortcut_warning("Ctrl+Alt+Esc") == shortcut_problem("Ctrl+Alt+Esc")  # the warning line too


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


def test_our_own_paste_and_enter_never_fire_the_shortcut(monkeypatch) -> None:
    # The paste's Ctrl+V and the Enter after it are injected keys too: an
    # Enter shortcut fired on them and could start a new recording. The hook
    # runs in its own process, which can't see when the app pastes, so each
    # key the app sends carries the marker instead.
    listener, send = keyboard_hook(monkeypatch, "Enter")
    events: list[str] = []
    listener.pressed.connect(lambda: events.append("pressed"))
    down, up, enter, injected = 0x0100, 0x0101, (0x0D, 0x1C), 0x10
    send(down, *enter, flags=injected, extra=hotkey_module.REPLAY_MARKER)
    send(up, *enter, flags=injected, extra=hotkey_module.REPLAY_MARKER)
    assert events == []

    send(down, *enter, flags=injected)  # a tool such as AutoHotkey may still press the shortcut
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


def test_esc_is_kept_from_the_game_only_while_held_back(monkeypatch) -> None:
    # Esc cancels a recording; while recording, it no longer also opens the
    # game's menu.
    listener, send = keyboard_hook(monkeypatch, "Ctrl+Alt+Q")
    cancels: list[bool] = []
    listener.cancelled.connect(lambda: cancels.append(True))
    down, up, esc = 0x0100, 0x0101, 0x1B
    assert send(down, esc, 0x01) == "passed" and len(cancels) == 1  # as usual
    assert send(up, esc, 0x01) == "passed"
    listener.set_escape_held_back(True)  # recording
    assert send(down, esc, 0x01) == "withheld" and len(cancels) == 2
    listener.set_escape_held_back(False)  # the cancel ended the recording
    assert send(up, esc, 0x01) == "withheld"  # its key-up goes with its key-down
    assert send(down, esc, 0x01) == "passed" and len(cancels) == 3
    listener.stop()


def test_the_retype_shortcut_fires_once_per_press(monkeypatch) -> None:
    # A missed dictation can be typed again with a second shortcut.
    listener, send = keyboard_hook(monkeypatch, "Ctrl+Alt+Q")
    events: list[str] = []
    listener.pressed.connect(lambda: events.append("pressed"))
    listener.retype_requested.connect(lambda: events.append("retype"))
    listener.update_retype_shortcut("Ctrl+Alt+V")
    down, sysdown, sysup = 0x0100, 0x0104, 0x0105
    ctrl, alt, v, q = (0xA2, 0x1D), (0xA4, 0x38), (0x56, 0x2F), (0x51, 0x10)
    send(down, *ctrl)
    send(sysdown, *alt)
    send(sysdown, *v)
    send(sysdown, *v)  # key repeat
    assert events == ["retype"]
    send(sysup, *v)
    send(sysdown, *v)
    send(sysup, *v)
    send(sysdown, *q)  # the main shortcut is untouched by all that
    send(sysup, *q)
    assert events == ["retype", "retype", "pressed"]
    send(sysdown, *v, flags=0x10, extra=hotkey_module.REPLAY_MARKER)  # our paste's V, Ctrl and Alt held
    assert events == ["retype", "retype", "pressed"]
    listener.update_retype_shortcut("")  # off
    send(sysdown, *v)
    assert events == ["retype", "retype", "pressed"]
    with pytest.raises(ValueError):
        listener.update_retype_shortcut("M4")  # keyboard shortcuts only
    listener.stop()


def test_side_button_holds_dictation_and_is_kept_from_other_apps(clock) -> None:
    listener, events, mouse = listening("M4")

    listener._mouse_filter(WM_XBUTTONDOWN, M4)
    clock.now += 1
    listener._mouse_filter(WM_XBUTTONUP, M4)

    assert events == ["pressed", "released"]
    assert mouse.suppressed == 2  # neither click reaches the browser's Back
    listener._cancel_watchdog_locked()


def test_other_buttons_and_missing_modifiers_pass_through(clock) -> None:
    listener, events, mouse = listening("Ctrl+M4")

    listener._mouse_filter(WM_XBUTTONDOWN, M5)
    listener._mouse_filter(WM_XBUTTONDOWN, M4)  # Ctrl is not held
    listener._mouse_filter(WM_XBUTTONUP, M4)
    assert events == [] and mouse.suppressed == 0

    listener._press_token("ctrl")
    listener._mouse_filter(WM_XBUTTONDOWN, M4)
    clock.now += 1
    listener._mouse_filter(WM_XBUTTONUP, M4)
    assert events == ["pressed", "released"]
    assert mouse.suppressed == 2
    listener._cancel_watchdog_locked()


def test_the_watchdog_never_types_a_hold_that_is_still_going(clock, watchdogs) -> None:
    # B2: 60 s into a long M3 hold the watchdog "released" it: the first part
    # was typed and Enter pressed, and letting go then did nothing.
    listener, events, mouse = listening("M3")
    listener._mouse_filter(WM_MBUTTONDOWN, PLAIN)
    clock.now += 30
    listener._mouse_filter(WM_MOUSEMOVE, PLAIN)  # still playing: the hooks are alive
    clock.now += 30
    watchdogs[-1].fire()
    assert events == ["pressed"] and watchdogs[-1].seconds == 60  # re-armed
    clock.now += 60  # nothing seen since: the release may have gone to an admin window
    watchdogs[-1].fire()
    assert events == ["pressed", "timed_out"]  # stopped, and nothing typed
    listener._mouse_filter(WM_MBUTTONUP, PLAIN)
    assert events == ["pressed", "timed_out"] and mouse.suppressed == 2


def test_a_hold_ends_at_the_hard_cap_however_busy_the_hooks_are(clock, watchdogs) -> None:
    listener, events, _mouse = listening("M3")
    listener._mouse_filter(WM_MBUTTONDOWN, PLAIN)
    for _ in range(10):
        if "timed_out" in events:
            break
        watchdog = watchdogs[-1]
        clock.now += watchdog.seconds - 1
        listener._mouse_filter(WM_MOUSEMOVE, PLAIN)
        clock.now += 1
        watchdog.fire()
    assert events == ["pressed", "timed_out"]
    assert clock.now - 1000.0 == hotkey_module.MAX_HOLD_SECONDS


def test_a_late_watchdog_cannot_end_the_next_hold(clock, watchdogs) -> None:
    listener, events, _mouse = listening("M3")
    listener._mouse_filter(WM_MBUTTONDOWN, PLAIN)
    late = watchdogs[-1]  # already firing as the hold ends
    clock.now += 1
    listener._mouse_filter(WM_MBUTTONUP, PLAIN)
    clock.now += 1
    listener._mouse_filter(WM_MBUTTONDOWN, PLAIN)
    late.fire()
    assert events == ["pressed", "released", "pressed"]


def test_a_failing_hook_callback_is_logged_not_fatal(monkeypatch) -> None:
    # IN-5: pynput stops a listener for good when one of its callbacks
    # raises, and join() re-raises it later, while the app says "ready".
    import ctypes

    from pynput import keyboard, mouse
    from pynput._util.win32 import SystemHook

    made: list = []
    monkeypatch.setattr(keyboard.Listener, "start", lambda self: made.append(self))
    monkeypatch.setattr(mouse.Listener, "start", lambda self: made.append(self))
    listener = HotkeyListener("M3")
    logged: list[str] = []  # the helper process sends these to the app's log
    listener.logged.connect(logged.append)
    listener.start()
    keyboard_hook, mouse_hook = made

    def broken(message: str):
        def fail(*args):
            raise RuntimeError(message)

        return fail

    monkeypatch.setattr(listener, "_press_token", broken("mouse hook broke"))
    monkeypatch.setattr(listener, "_canonical_token", broken("key callback broke"))
    try:
        mouse_hook._handler(0, WM_MBUTTONDOWN, ctypes.pointer(mouse_hook._MSLLHOOKSTRUCT()))
    except SystemHook.SuppressException:
        pass
    keyboard_hook._process(0x0100, 0x51)  # Q down
    assert keyboard_hook._queue.empty() and mouse_hook._queue.empty()  # nothing kills them
    assert "hotkey: _mouse_filter failed: RuntimeError('mouse hook broke')" in logged
    assert "hotkey: _on_press failed: RuntimeError('key callback broke')" in logged
    listener.stop()


def test_a_hook_that_died_with_an_error_cannot_orphan_the_other(hooks) -> None:
    # IN-5: what join() re-raised skipped the clean-up after it, so the
    # hooks never came back, and a live one was orphaned.
    listener = HotkeyListener("M3")
    listener.start()
    keyboard_hook, mouse_hook = hooks
    keyboard_hook.failure = RuntimeError("a callback failed")
    listener.rehook()
    assert [hook.live for hook in hooks] == [False, False, True, True]
    hooks[2].failure = RuntimeError("again")
    listener.stop()  # Dictation off
    assert not any(hook.live for hook in hooks)


def test_rehook_puts_in_fresh_hooks_and_lets_go_of_a_hold(hooks, watchdogs) -> None:
    # IN-6: Windows silently drops a hook whose callback ran too long, and a
    # dead M3 hook can't start the reload that would put it back. The window
    # re-hooks after unlock and resume.
    listener = HotkeyListener("M3")
    events: list[str] = []
    listener.released.connect(lambda hwnd: events.append("released"))
    listener.timed_out.connect(lambda: events.append("timed_out"))
    listener.start()
    listener._mouse_filter(WM_MBUTTONDOWN, PLAIN)  # held as the PC locked
    listener.rehook()
    assert [hook.live for hook in hooks] == [False, False, True, True]
    assert events == ["timed_out"]  # its release went to the lock screen
    listener.stop()


def test_dead_or_deaf_hooks_are_put_back(hooks, clock, monkeypatch) -> None:
    listener = HotkeyListener("M3")
    listener.start()
    assert listener._health.active  # a timer every few seconds, never inside a hook
    idle: list[float | None] = [None]
    monkeypatch.setattr(hotkey_module, "_input_idle_seconds", lambda: idle[0])
    listener._check_hooks()
    assert len(hooks) == 2  # healthy: left alone

    hooks[0].live = False  # pynput's keyboard thread died
    listener._check_hooks()
    assert len(hooks) == 4 and hooks[2].live and hooks[3].live

    clock.now += 10
    listener._mouse_filter(WM_MOUSEMOVE, PLAIN)  # these hooks saw input at 1010
    clock.now += 10
    idle[0] = 9.0  # so did Windows, at 1011
    listener._check_hooks()
    assert len(hooks) == 4
    idle[0] = 5.0  # Windows saw input at 1015 that the hooks missed
    listener._check_hooks()
    assert len(hooks) == 6
    listener._check_hooks()  # fresh hooks that saw nothing yet: an admin window has the input
    assert len(hooks) == 6

    hooks[-1].ident = 4242  # pynput never checks what SetWindowsHookExW returned
    monkeypatch.setitem(hotkey_module.SystemHook._HOOKS, 4242, SimpleNamespace(_hook=0))
    listener._check_hooks()
    assert len(hooks) == 8
    listener.stop()
    assert not listener._health.active

    keys = HotkeyListener("Ctrl+Alt+Q")  # only the keyboard hooked
    keys.start()
    clock.now += 1
    keys._keyboard_filter(0x0100, SimpleNamespace(flags=0, vkCode=0x41, scanCode=0x1E, dwExtraInfo=None))
    clock.now += 10
    idle[0] = 0.0  # the mouse moved, which the keyboard hook never sees
    keys._check_hooks()
    assert len(hooks) == 9
    keys.stop()


def test_a_short_m3_tap_stays_a_middle_click(clock, send_input) -> None:
    # M3 never reached apps, and an accidental tap showed an error.
    listener, events, mouse = listening("M3")
    listener._mouse_filter(WM_MBUTTONDOWN, PLAIN)
    clock.now += hotkey_module.TAP_SECONDS / 2
    listener._mouse_filter(WM_MBUTTONUP, PLAIN)
    assert events == ["pressed", "tapped"] and mouse.suppressed == 2
    assert send_input.sent.wait(2)
    marker = hotkey_module.REPLAY_MARKER
    assert send_input.clicks == [[(0x20, 0, marker), (0x40, 0, marker)]]  # middle down, up
    replayed = SimpleNamespace(mouseData=0, dwExtraInfo=marker)  # back through our hook
    listener._mouse_filter(WM_MBUTTONDOWN, replayed)
    listener._mouse_filter(WM_MBUTTONUP, replayed)
    assert events == ["pressed", "tapped"] and mouse.suppressed == 2  # to the apps

    listener._mouse_filter(WM_MBUTTONDOWN, PLAIN)
    clock.now += hotkey_module.TAP_SECONDS
    listener._mouse_filter(WM_MBUTTONUP, PLAIN)
    assert events[2:] == ["pressed", "released"]  # a hold dictates


def test_a_side_button_tap_replays_that_button(clock, send_input) -> None:
    listener, events, _mouse = listening("Ctrl+M5")
    listener._press_token("ctrl")
    listener._mouse_filter(WM_XBUTTONDOWN, M5)
    listener._mouse_filter(WM_XBUTTONUP, M5)
    assert events == ["pressed", "tapped"]
    assert send_input.sent.wait(2)
    marker = hotkey_module.REPLAY_MARKER
    assert send_input.clicks == [[(0x80, 2, marker), (0x100, 2, marker)]]  # XBUTTON2

    keys, key_events, _mouse = listening("F9")  # keyboard triggers are unchanged
    keys._press_token("f9")
    keys._release_token("f9")
    assert key_events == ["pressed", "released"]


def test_same_shortcut_compares_the_keys_not_the_spelling() -> None:
    # A dictation shortcut equal to the retype one fired both on one press.
    assert hotkey_module.same_shortcut("Ctrl+Alt+V", "alt + ctrl + v")
    assert not hotkey_module.same_shortcut("Ctrl+Alt+V", "Ctrl+Alt+Q")
    assert not hotkey_module.same_shortcut("Ctrl+Alt+V", "")
