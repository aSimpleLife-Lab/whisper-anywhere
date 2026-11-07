from __future__ import annotations

import ctypes
import threading
import time
from ctypes import windll, wintypes
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal

try:
    import win32gui
except Exception:  # pragma: no cover - Windows-only dependency
    win32gui = None

try:
    from pynput import keyboard as pynput_keyboard
    from pynput import mouse as pynput_mouse
    from pynput._util.win32 import INPUT, INPUT_union, MOUSEINPUT, SendInput, SystemHook
except Exception:  # pragma: no cover - dependency missing at runtime
    pynput_keyboard = None
    pynput_mouse = None

WATCHDOG_SECONDS = 60.0
MAX_HOLD_SECONDS = 300.0  # however busy the hooks are, a hold ends here
HEALTH_CHECK_SECONDS = 3.0  # how often _check_hooks looks for a dead hook
# Windows saw input this long after the last event our hooks saw: they died.
HOOK_SILENCE_SECONDS = 2.0
# Low-level mouse hook messages for the bindable buttons: (token, pressed).
MOUSE_MESSAGES = {
    0x0201: ("mouse1", True),
    0x0202: ("mouse1", False),
    0x0204: ("mouse2", True),
    0x0205: ("mouse2", False),
    0x0207: ("mouse3", True),
    0x0208: ("mouse3", False),
}
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
TAP_SECONDS = 0.25  # a mouse trigger let go sooner is a plain click
# SendInput flags (down, up) and mouseData that replay a tapped button.
MOUSE_REPLAY = {
    "mouse1": (0x0002, 0x0004, 0),
    "mouse2": (0x0008, 0x0010, 0),
    "mouse3": (0x0020, 0x0040, 0),
    "mouse4": (0x0080, 0x0100, 1),  # XBUTTON1
    "mouse5": (0x0080, 0x0100, 2),  # XBUTTON2
}
# "WHIS" in dwExtraInfo: input we sent ourselves, a replayed tap or the
# paste, Enter and typing of TextInserter. The hooks let it pass untouched.
REPLAY_MARKER = 0x57484953
VK_PACKET = 0xE7  # SendInput Unicode characters arrive with this virtual key
VK_ESCAPE = 0x1B
VK_TAB = 0x09
VK_LCONTROL = 0xA2
VK_RMENU = 0xA5
ALTGR_CTRL_SCAN = 0x21D  # the left Ctrl Windows fakes in front of AltGr
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
# Windows' own state of each modifier, which never misses a key-up.
MODIFIER_VKS = {"ctrl": (0x11,), "shift": (0x10,), "alt": (0x12,), "win": (0x5B, 0x5C)}
# M1 and M2 are ordinary clicking; bound alone they would block the mouse.
MOUSE_NEEDS_MODIFIER = {"mouse1", "mouse2"}
# Bound alone, these fire while typing.
TYPING_KEYS = {"enter", "tab", "backspace", "space", *"abcdefghijklmnopqrstuvwxyz", *"0123456789"}

KEY_NAME_TO_VK = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}
for letter in "abcdefghijklmnopqrstuvwxyz":
    KEY_NAME_TO_VK[letter] = ord(letter.upper())
for digit in "0123456789":
    KEY_NAME_TO_VK[digit] = ord(digit)
for number in range(1, 13):
    KEY_NAME_TO_VK[f"f{number}"] = 0x6F + number
KEY_NAME_TO_VK.update(
    {
        "semicolon": 0xBA,
        "equals": 0xBB,
        "comma": 0xBC,
        "minus": 0xBD,
        "period": 0xBE,
        "slash": 0xBF,
        "backtick": 0xC0,
        "leftbracket": 0xDB,
        "backslash": 0xDC,
        "rightbracket": 0xDD,
        "quote": 0xDE,
        # Mouse buttons: M1 left, M2 right, M3 middle, M4/M5 the side buttons.
        "mouse1": 0x01,
        "mouse2": 0x02,
        "mouse3": 0x04,
        "mouse4": 0x05,
        "mouse5": 0x06,
    }
)

DISPLAY_MODIFIER = {
    "ctrl": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "win": "Win",
}

DISPLAY_TRIGGER_NAMES = {
    "space": "Space",
    "enter": "Enter",
    "tab": "Tab",
    "esc": "Esc",
    "backspace": "Backspace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}
for number in range(1, 13):
    DISPLAY_TRIGGER_NAMES[f"f{number}"] = f"F{number}"
for number in range(1, 6):
    DISPLAY_TRIGGER_NAMES[f"mouse{number}"] = f"M{number}"
DISPLAY_TRIGGER_NAMES.update(
    {
        "semicolon": "Semicolon",
        "equals": "Equals",
        "comma": "Comma",
        "minus": "Minus",
        "period": "Period",
        "slash": "Slash",
        "backtick": "Backtick",
        "leftbracket": "LeftBracket",
        "backslash": "Backslash",
        "rightbracket": "RightBracket",
        "quote": "Quote",
    }
)

CHAR_TRIGGER_ALIASES = {
    ";": "semicolon",
    "=": "equals",
    ",": "comma",
    "-": "minus",
    ".": "period",
    "/": "slash",
    "`": "backtick",
    "[": "leftbracket",
    "\\": "backslash",
    "]": "rightbracket",
    "'": "quote",
}

TRIGGER_TOKEN_ALIASES = {
    "return": "enter",
    "escape": "esc",
    **{f"m{number}": f"mouse{number}" for number in range(1, 6)},
    **CHAR_TRIGGER_ALIASES,
}

PYNPUT_KEY_TOKENS: dict[object, str] = {}
if pynput_keyboard is not None:

    def _register_pynput_key(token: str, *names: str) -> None:
        for name in names:
            key = getattr(pynput_keyboard.Key, name, None)
            if key is not None:
                PYNPUT_KEY_TOKENS[key] = token

    _register_pynput_key("ctrl", "ctrl", "ctrl_l", "ctrl_r")
    _register_pynput_key("shift", "shift", "shift_l", "shift_r")
    _register_pynput_key("alt", "alt", "alt_l", "alt_r", "alt_gr")
    _register_pynput_key("win", "cmd", "cmd_l", "cmd_r")
    _register_pynput_key("space", "space")
    _register_pynput_key("enter", "enter")
    _register_pynput_key("tab", "tab")
    _register_pynput_key("esc", "esc")
    _register_pynput_key("backspace", "backspace")
    _register_pynput_key("delete", "delete")
    _register_pynput_key("insert", "insert")
    _register_pynput_key("home", "home")
    _register_pynput_key("end", "end")
    _register_pynput_key("pageup", "page_up")
    _register_pynput_key("pagedown", "page_down")
    _register_pynput_key("up", "up")
    _register_pynput_key("down", "down")
    _register_pynput_key("left", "left")
    _register_pynput_key("right", "right")
    for number in range(1, 13):
        _register_pynput_key(f"f{number}", f"f{number}")


@dataclass(frozen=True)
class ParsedShortcut:
    modifiers: frozenset[str]
    trigger_vk: int | None
    trigger_name: str | None
    trigger_token: str | None

    @property
    def display(self) -> str:
        parts = [
            DISPLAY_MODIFIER[name]
            for name in ("ctrl", "shift", "alt", "win")
            if name in self.modifiers
        ]
        if self.trigger_name:
            parts.append(self.trigger_name)
        return "+".join(parts)

    @property
    def mouse_trigger(self) -> str | None:
        token = self.trigger_token or ""
        return token if token.startswith("mouse") else None

    def involved_vks(self) -> set[int]:
        values: set[int] = set()
        for modifier in self.modifiers:
            if modifier == "ctrl":
                values.update({0x11, 0xA2, 0xA3})
            elif modifier == "shift":
                values.update({0x10, 0xA0, 0xA1})
            elif modifier == "alt":
                values.update({0x12, 0xA4, 0xA5})
            elif modifier == "win":
                values.update({0x5B, 0x5C})
        if self.trigger_vk is not None:
            values.add(self.trigger_vk)
        return values


def parse_retype_shortcut(shortcut_text: str) -> ParsedShortcut | None:
    """The retype shortcut, or None when it is off. Keyboard only: the mouse
    is hooked only for a mouse dictation shortcut."""
    parsed = parse_shortcut(shortcut_text) if shortcut_text.strip() else None
    if parsed is not None and parsed.mouse_trigger:
        raise ValueError("Use a keyboard shortcut to type the last dictation again.")
    return parsed


def same_shortcut(first: str, second: str) -> bool:
    """Whether two shortcut texts are the same keys, however they are written."""
    try:
        one, other = parse_shortcut(first), parse_shortcut(second)
    except ValueError:
        return False
    return (one.modifiers, one.trigger_token) == (other.modifiers, other.trigger_token)


def _normalize_trigger_token(part: str) -> str:
    token = TRIGGER_TOKEN_ALIASES.get(part.replace(" ", ""), part.replace(" ", ""))
    if token in KEY_NAME_TO_VK:
        return token
    raise ValueError(f"Unsupported shortcut key: {part}")


def _display_trigger_name(raw_part: str, token: str) -> str:
    if len(raw_part) == 1:
        return raw_part.upper()
    return DISPLAY_TRIGGER_NAMES.get(token, raw_part.title())


def parse_shortcut(shortcut_text: str) -> ParsedShortcut:
    parts = [
        part.strip()
        for part in shortcut_text.replace("-", "+").split("+")
        if part.strip()
    ]
    if not parts:
        raise ValueError("Enter a shortcut such as Ctrl+Alt+Q.")

    modifiers: set[str] = set()
    trigger_vk: int | None = None
    trigger_name: str | None = None
    trigger_token: str | None = None

    for raw_part in parts:
        part = raw_part.lower()
        if part in ("control", "ctrl"):
            modifiers.add("ctrl")
        elif part in ("windows", "win", "cmd", "meta"):
            modifiers.add("win")
        elif part in ("shift",):
            modifiers.add("shift")
        elif part in ("alt", "option"):
            modifiers.add("alt")
        else:
            if trigger_vk is not None:
                raise ValueError("Use only one non-modifier key in a shortcut.")
            trigger_token = _normalize_trigger_token(part)
            trigger_vk = KEY_NAME_TO_VK[trigger_token]
            trigger_name = _display_trigger_name(raw_part, trigger_token)

    if not modifiers and trigger_vk is None:
        raise ValueError("Shortcut must include Ctrl, Win, Alt, or Shift.")
    if trigger_token in MOUSE_NEEDS_MODIFIER and not modifiers:
        raise ValueError(
            f"{trigger_name} is an ordinary click. Pair it with Ctrl, Alt, Shift or Win, or use M3, M4 or M5."
        )

    return ParsedShortcut(frozenset(modifiers), trigger_vk, trigger_name, trigger_token)


def _modifier_down(token: str) -> bool:
    return any(windll.user32.GetAsyncKeyState(vk) & 0x8000 for vk in MODIFIER_VKS[token])


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _input_idle_seconds() -> float | None:
    """How long ago Windows saw keyboard or mouse input, or None."""
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
    if not windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return ((windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def _hook_missing(listener: object) -> bool:
    """pynput never checks SetWindowsHookExW: a 0 handle is no hook at all."""
    hook = SystemHook._HOOKS.get(getattr(listener, "ident", None))
    return hook is not None and hook._hook == 0


def _replay_click(token: str) -> None:
    """Send a tapped button to the apps as a plain click, marked so our own
    mouse hook lets it through."""
    down, up, data = MOUSE_REPLAY[token]
    clicks = (INPUT * 2)(
        *(
            INPUT(
                type=INPUT.MOUSE,
                value=INPUT_union(
                    mi=MOUSEINPUT(mouseData=data, dwFlags=flags, dwExtraInfo=REPLAY_MARKER)
                ),
            )
            for flags in (down, up)
        )
    )
    SendInput(2, clicks, ctypes.sizeof(INPUT))


def shortcut_problem(shortcut_text: str, retype: bool = False) -> str:
    """Why a shortcut can't be saved, or "" when it can. shortcut_warning
    covers the softer cases, which the window asks about first. Tab alone
    can dictate, as the hook holds it back from apps; the retype shortcut's
    keys reach apps, so there it would fire while you type."""
    try:
        parsed = parse_shortcut(shortcut_text)
    except ValueError as exc:
        return str(exc)
    if parsed.trigger_token == "esc":
        return "Esc cancels a recording, so a shortcut with Esc would cancel itself. Choose another key."
    typing_key = parsed.trigger_token in TYPING_KEYS and (retype or parsed.trigger_token != "tab")
    if not parsed.modifiers and typing_key:
        return f"{parsed.display} on its own fires while you type. Add Ctrl, Alt, Shift or Win."
    return ""


def shortcut_warning(shortcut_text: str) -> str:
    problem = shortcut_problem(shortcut_text)  # parse errors included
    if problem:
        return problem
    parsed = parse_shortcut(shortcut_text)
    display = parsed.display.lower().replace(" ", "")

    common = {
        "ctrl+a": "Ctrl+A selects all in most apps.",
        "ctrl+c": "Ctrl+C is copy in most apps.",
        "ctrl+f": "Ctrl+F opens find in most apps.",
        "ctrl+n": "Ctrl+N creates a new item/window in many apps.",
        "ctrl+o": "Ctrl+O opens files in many apps.",
        "ctrl+p": "Ctrl+P prints in many apps.",
        "ctrl+s": "Ctrl+S saves in most apps.",
        "ctrl+v": "Ctrl+V is paste in most apps.",
        "ctrl+w": "Ctrl+W closes browser/editor tabs.",
        "ctrl+x": "Ctrl+X is cut in most apps.",
        "ctrl+y": "Ctrl+Y is redo in many apps.",
        "ctrl+z": "Ctrl+Z is undo in most apps.",
        "ctrl+alt+delete": "Ctrl+Alt+Delete is reserved by Windows.",
        "ctrl+alt+del": "Ctrl+Alt+Delete is reserved by Windows.",
        "alt+f4": "Alt+F4 closes the active window.",
        "alt+tab": "Alt+Tab switches apps in Windows.",
        "alt+space": "Alt+Space opens the window menu.",
        "win+a": "Win+A opens Windows Quick Settings.",
        "win+l": "Win+L locks Windows.",
        "win+d": "Win+D shows the desktop.",
        "win+r": "Win+R opens Run.",
        "win+e": "Win+E opens File Explorer.",
        "win+i": "Win+I opens Windows Settings.",
        "win+s": "Win+S opens Windows Search.",
        "win+tab": "Win+Tab opens Task View.",
    }
    if display in common:
        return common[display]
    if parsed.mouse_trigger:
        # The bound button is held back from apps, so say what that costs.
        if parsed.mouse_trigger in MOUSE_NEEDS_MODIFIER:
            return f"{parsed.display} no longer reaches apps while it is the dictation shortcut."
        if parsed.mouse_trigger == "mouse3" and not parsed.modifiers:
            return "Middle-click no longer reaches apps while M3 is the dictation shortcut."
        return ""
    if parsed.trigger_token == "tab" and not parsed.modifiers:
        return "Tab no longer reaches apps while it is the dictation shortcut; Alt+Tab, Shift+Tab and Ctrl+Tab still do."
    if not parsed.modifiers and parsed.trigger_vk is not None:
        return "Single-key shortcuts can trigger while typing. Use a modifier if it gets in the way."
    if parsed.trigger_vk is None:
        return "Modifier-only shortcuts are unreliable. Ctrl+Alt+Q is recommended."
    if (
        parsed.trigger_token in {"tab", "enter", "backspace", "delete", "space"}
        and len(parsed.modifiers) < 2
    ):
        return "This key is used heavily by Windows and apps. Add two modifiers or use Ctrl+Alt+Q."
    if (
        parsed.trigger_token
        and parsed.trigger_token.startswith("f")
        and parsed.trigger_token in {"f1", "f5", "f11", "f12"}
    ):
        return "This function key is commonly used by Windows, browsers, or developer tools."
    if "win" in parsed.modifiers:
        return "Windows-key shortcuts can be intercepted by the shell on some systems. Ctrl+Alt+Q is the safest default."
    if parsed.modifiers == frozenset({"ctrl"}) or parsed.modifiers == frozenset(
        {"alt"}
    ):
        return "Single-modifier shortcuts often conflict with app menus and editor/browser shortcuts. Ctrl+Alt+Q is recommended."
    return ""


class HotkeyListener(QObject):
    """The low-level keyboard and mouse hooks. They run in the hotkey helper
    process (core.hotkey_helper), which sends these signals on to the app."""

    pressed = Signal()
    released = Signal(object)  # the window in front at a real release
    cancelled = Signal()
    timed_out = Signal()  # a hold whose release may have been missed: never type it
    tapped = Signal()  # a mouse trigger let go within TAP_SECONDS: a plain click
    retype_requested = Signal()  # the retype shortcut, once per press
    error = Signal(str)
    status = Signal(str)
    logged = Signal(str)  # a line for the app's log

    def __init__(self, shortcut: str = "Ctrl+Alt+Q", mode: str = "hold", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.shortcut = parse_shortcut(shortcut)
        self.mode = mode if mode in ("hold", "toggle") else "hold"
        self._required_tokens = self._required_tokens_for(self.shortcut)
        self._retype_tokens: frozenset[str] = frozenset()
        self._pressed_tokens: set[str] = set()
        self._shortcut_active = False
        self._running = False
        self._listener: object | None = None
        self._mouse_listener: object | None = None
        self._mouse_held = False
        self._tab_held = False
        self._altgr_down = False
        self._escape_held_back = False
        self._escape_swallowed = False
        self._last_input_at = 0.0  # the last event either hook saw
        self._hooked_at = 0.0  # when the hooks were last put in
        self._held_since = 0.0  # when the current hold began
        self._watchdog_armed_at = 0.0
        self._watchdog: threading.Timer | None = None
        self._state_lock = threading.Lock()
        self._hooks_lock = threading.RLock()  # guards the two pynput listeners
        self._health = QTimer(self)
        self._health.setInterval(int(HEALTH_CHECK_SECONDS * 1000))
        self._health.timeout.connect(self._check_hooks)

    def start(self) -> None:
        if self._running:
            return
        if pynput_keyboard is None:
            message = "The global shortcut dependency is missing. Reinstall Whisper Anywhere to restore hotkeys."
            self._log(f"Hotkey start failed: {message}")
            self.error.emit(message)
            self.status.emit(message)
            return

        self._log(
            f"Starting pynput hotkey listener: shortcut={self.shortcut.display}, mode={self.mode}"
        )
        self.status.emit("Starting global shortcut listener.")
        self._running = True
        with self._hooks_lock:
            error = self._start_keyboard_hook()
            if not error:
                self._sync_mouse_listener()
        if error:
            self._running = False
            message = f"Could not start the global shortcut listener. {error}"
            self._log(message)
            self.error.emit(message)
            self.status.emit(message)
            return
        self._health.start()
        self.status.emit(f"Global shortcut ready: {self.shortcut.display}.")

    def _start_keyboard_hook(self) -> str:
        """Hook the keyboard. Returns an error message, or "" when there is
        nothing to report."""
        with self._hooks_lock:
            if self._listener is not None:
                return ""
            try:
                listener = pynput_keyboard.Listener(
                    on_press=lambda key: self._guarded(self._on_press, key),
                    on_release=lambda key: self._guarded(self._on_release, key),
                    win32_event_filter=lambda msg, data: self._guarded(self._keyboard_filter, msg, data),
                )
                listener.daemon = True
                listener.start()
            except Exception as exc:
                return str(exc)
            self._listener = listener
            self._hooked_at = time.monotonic()
        self._log("pynput keyboard listener started")
        return ""

    def _guarded(self, callback, *args):
        """Run a hook callback. pynput stops a listener for good when one
        raises, and join() re-raises it later: log it instead."""
        try:
            return callback(*args)
        except SystemHook.SuppressException:  # how suppress_event() works
            raise
        except Exception as exc:
            self._log(f"{callback.__name__} failed: {exc!r}")
            return None

    def _unhook(self) -> None:
        """Drop both hooks. join() re-raises whatever killed a listener, which
        must not leave the other hook in or skip the caller's clean-up."""
        with self._hooks_lock:
            hooks = (self._listener, self._mouse_listener)
            self._listener = self._mouse_listener = None
            self._mouse_held = False
        for hook in hooks:
            if hook is None:
                continue
            try:
                hook.stop()
                if hook.is_alive():
                    hook.join(timeout=2)  # gone before a fresh one goes in
            except Exception as exc:
                self._log(f"A hook stopped with an error: {exc!r}")

    def _hook_again(self) -> None:
        """Hook again. Releases were missed while unhooked: a held shortcut
        counts as let go."""
        error = ""
        with self._hooks_lock:
            if self._running:
                error = self._start_keyboard_hook()
                self._sync_mouse_listener()
        if error:
            self.error.emit(f"Could not restart the global shortcut listener. {error}")
        if self.mode == "hold":
            self._force_release()
        else:
            with self._state_lock:
                self._pressed_tokens.clear()
                self._shortcut_active = False

    def rehook(self) -> None:
        """Drop both hooks and put in fresh ones. Windows silently removes a
        low-level hook whose callback ran too long, and a dead M3 hook can't
        start the model reload that would put it back. The window calls this
        after session unlock and resume, the health check when one died."""
        self._unhook()
        self._hook_again()
        self._log(f"Hooks put in again: running={self._running}")

    def _check_hooks(self) -> None:
        """Re-hook when a hook died. Runs on a timer, never inside a hook."""
        with self._hooks_lock:
            if not self._running:
                return
            hooks = [hook for hook in (self._listener, self._mouse_listener) if hook is not None]
            mouse_hooked = self._mouse_listener is not None
        dead = any(not hook.is_alive() or _hook_missing(hook) for hook in hooks)
        # While the mouse is hooked every key and mouse event passes one of
        # our hooks, so input Windows saw and they missed means one died.
        # Fresh hooks that saw nothing yet are left alone: an admin window or
        # the lock screen has the input, and hooking again would not help.
        # ponytail: with only the keyboard hooked, mouse input never reaches
        # us, so a dead keyboard hook is caught only once its thread dies;
        # an always-on mouse hook would lift that, at a Python call per move.
        idle = _input_idle_seconds() if mouse_hooked else None
        missed = (
            idle is not None
            and self._last_input_at > self._hooked_at
            and time.monotonic() - idle - self._last_input_at > HOOK_SILENCE_SECONDS
        )
        if dead or missed:
            self._log(f"Hook check: dead={dead}, missed input={missed}; hooking again")
            self.rehook()

    def set_escape_held_back(self, held: bool) -> None:
        """While the window records, Esc still cancels but is withheld from
        apps, so a game's menu doesn't open; otherwise it reaches them."""
        self._escape_held_back = held

    def _sync_mouse_listener(self) -> None:
        """Hook the mouse only while a mouse button is the trigger. Callers
        hold _hooks_lock."""
        wanted = self._running and self.shortcut.mouse_trigger is not None
        if wanted and self._mouse_listener is None and pynput_mouse is not None:
            try:
                listener = pynput_mouse.Listener(
                    win32_event_filter=lambda msg, data: self._guarded(self._mouse_filter, msg, data)
                )
                listener.daemon = True
                listener.start()
            except Exception as exc:
                message = f"Could not watch the mouse for {self.shortcut.display}. {exc}"
                self._log(message)
                self.error.emit(message)
                return
            self._mouse_listener = listener
            self._hooked_at = time.monotonic()
            self._log("pynput mouse listener started")
        elif not wanted and self._mouse_listener is not None:
            listener = self._mouse_listener
            self._mouse_listener = None
            self._mouse_held = False
            listener.stop()
            self._log("pynput mouse listener stopped")

    def _keyboard_filter(self, msg: int, data: object) -> bool:
        """Runs inside the low-level keyboard hook; False keeps an event from
        the shortcut (apps still get it). Layouts with AltGr send that key as a
        fake left Ctrl plus Right Alt, so AltGr+Q ('@' on a German keyboard)
        read as Ctrl+Alt+Q. Unicode typing arrives as VK_PACKET. Our own paste
        and Enter fired a V or Enter shortcut: they carry REPLAY_MARKER. Keys
        other tools inject, such as AutoHotkey's, still count."""
        self._last_input_at = time.monotonic()  # the hooks are alive
        if data.dwExtraInfo == REPLAY_MARKER:
            return False
        vk = data.vkCode
        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        if vk == VK_LCONTROL and data.scanCode == ALTGR_CTRL_SCAN:
            self._altgr_down = down
            return False
        if vk == VK_ESCAPE and (self._escape_held_back if down else self._escape_swallowed):
            # Withheld, Esc never reaches _on_press, so it cancels from here;
            # its key-up goes with its key-down.
            self._escape_swallowed = down
            if down and self._running:
                self.cancelled.emit()
            listener = self._listener
            if listener is not None:
                listener.suppress_event()
            return False
        if vk == VK_TAB and self._running and self.shortcut.trigger_token == "tab" and not self.shortcut.modifiers:
            return self._tab_shortcut(down)
        return vk != VK_PACKET and not (vk == VK_RMENU and self._altgr_down)

    def _tab_shortcut(self, down: bool) -> bool:
        """Tab alone as the shortcut is held back from apps, as a bound mouse
        button is: a Tab that reached the app would move the focus off the
        text box at every toggle. Tab with a modifier (Alt+Tab, Shift+Tab) is
        not the shortcut and reaches apps untouched.

        ponytail: only a bare Tab is held back; other bare keys (F9, Backtick)
        still reach apps, and a quick tap in hold mode isn't replayed as a
        Tab the way M3's is. Tab combos skip the shortcut logic, so a retype
        shortcut with Tab can't fire while Tab dictates."""
        if down and not self._tab_held:
            with self._state_lock:
                self._forget_released_modifiers_locked("tab")
                if self._pressed_tokens & MODIFIER_VKS.keys():
                    return False
            self._tab_held = True
            self._press_token("tab")
        elif not down:
            if not self._tab_held:
                return False
            self._tab_held = False
            self._release_token("tab")
        listener = self._listener
        if listener is not None:
            listener.suppress_event()
        return False

    def _mouse_filter(self, msg: int, data: object) -> bool:
        """Runs inside the low-level mouse hook, so ordinary events cost only a
        timestamp and a dict lookup. The bound button is handled here and withheld
        from other apps, so M4 starts dictation instead of going Back. A hold
        dictates; a tap is replayed to the apps as a plain click."""
        now = self._last_input_at = time.monotonic()  # the hooks are alive
        if msg in MOUSE_MESSAGES:
            token, down = MOUSE_MESSAGES[msg]
        elif msg in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            button = (int(getattr(data, "mouseData", 0)) >> 16) & 0xFFFF
            token, down = ("mouse4" if button == 1 else "mouse5"), msg == WM_XBUTTONDOWN
        else:
            return False
        if getattr(data, "dwExtraInfo", None) == REPLAY_MARKER:
            return False  # our replayed tap
        if token != self.shortcut.mouse_trigger or not self._running:
            return False
        if down:
            with self._state_lock:
                self._forget_released_modifiers_locked(token)
                modifiers_held = self.shortcut.modifiers.issubset(self._pressed_tokens)
            if not modifiers_held:
                return False
            self._mouse_held = True
            self._press_token(token)
        elif self._mouse_held:
            self._mouse_held = False
            self._release_token(token, tapped=self.mode == "hold" and now - self._held_since < TAP_SECONDS)
        else:
            return False
        listener = self._mouse_listener
        if listener is not None:
            listener.suppress_event()
        return False

    def stop(self) -> None:
        if not self._running and self._listener is None:
            return

        self._log("Stopping hotkey listener")
        self.status.emit("Stopping global shortcut listener.")
        self._running = False
        self._health.stop()
        with self._state_lock:
            self._pressed_tokens.clear()
            self._shortcut_active = False
            self._cancel_watchdog_locked()

        self._unhook()
        self.status.emit("Global shortcut listener stopped.")

    def update_shortcut(self, shortcut: str, mode: str) -> None:
        parsed = parse_shortcut(shortcut)
        with self._state_lock:
            self.shortcut = parsed
            self.mode = mode if mode in ("hold", "toggle") else "hold"
            self._required_tokens = self._required_tokens_for(parsed)
            self._pressed_tokens.clear()
            self._shortcut_active = False
            self._cancel_watchdog_locked()
        self._mouse_held = self._tab_held = False
        with self._hooks_lock:
            self._sync_mouse_listener()
        self._log(f"Updated hotkey: shortcut={self.shortcut.display}, mode={self.mode}")
        self.status.emit(f"Shortcut active: {self.shortcut.display} ({self.mode}).")

    def update_retype_shortcut(self, shortcut: str) -> None:
        """A second shortcut that asks the window to type the last dictation
        again; "" turns it off. Its keys still reach apps, as all keys do."""
        parsed = parse_retype_shortcut(shortcut)
        with self._state_lock:
            self._retype_tokens = self._required_tokens_for(parsed) if parsed else frozenset()
        self._log(f"Retype shortcut: {parsed.display if parsed else 'off'}")

    def _on_press(self, key: object) -> None:
        if not self._running:
            return

        token = self._canonical_token(key)
        if token is None:
            return

        if token == "esc":
            self.cancelled.emit()  # the window cancels a recording if one is running
        self._press_token(token)

    def _press_token(self, token: str) -> None:
        with self._state_lock:
            was_combo_active = self._shortcut_active
            self._forget_released_modifiers_locked(token)
            # The retype combo completes on a fresh key-down, never a key
            # repeat, and leaves the dictation shortcut's state alone.
            emit_retype = token not in self._pressed_tokens and token in self._retype_tokens
            self._pressed_tokens.add(token)
            emit_retype = emit_retype and self._retype_tokens <= self._pressed_tokens
            self._shortcut_active = self._is_combo_active_locked()
            emit_pressed = self._shortcut_active and not was_combo_active
            if emit_pressed and self.mode == "hold":
                self._held_since = time.monotonic()
                self._arm_watchdog_locked(WATCHDOG_SECONDS)

        if emit_retype:
            self._log("Retype shortcut pressed")
            self.retype_requested.emit()
        # In toggle mode the window starts or stops: it knows whether it is
        # recording, and a refused start used to leave a flag here inverted.
        # The cues play there too, only when the microphone really opens.
        if emit_pressed:
            self._log(f"{self.mode.title()} shortcut pressed")
            self.status.emit(f"Shortcut pressed: {self.shortcut.display}.")
            self.pressed.emit()

    def _on_release(self, key: object) -> None:
        if not self._running:
            return

        token = self._canonical_token(key)
        if token is None:
            return
        self._release_token(token)

    def _release_token(self, token: str, tapped: bool = False) -> None:
        emit_released = False
        with self._state_lock:
            was_combo_active = self._shortcut_active
            was_in_combo = token in self._required_tokens
            self._pressed_tokens.discard(token)
            is_combo_active = self._is_combo_active_locked()
            self._shortcut_active = is_combo_active
            if (
                self.mode == "hold"
                and was_combo_active
                and was_in_combo
                and not is_combo_active
            ):
                self._cancel_watchdog_locked()
                emit_released = True

        if emit_released and tapped:
            # Sent from a thread of its own: the hook must answer at once.
            threading.Thread(target=_replay_click, args=(token,), daemon=True).start()
            self._log("Hold shortcut tapped: replayed as a click")
            self.status.emit(f"Shortcut tapped: {self.shortcut.display} clicked as usual.")
            self.tapped.emit()
        elif emit_released:
            hwnd = self._capture_foreground_window()
            self._log(f"Hold shortcut released target={hwnd}")
            self.status.emit(f"Shortcut released: {self.shortcut.display}.")
            self.released.emit(hwnd)

    def _force_release(self, held_since: float | None = None) -> None:
        """End a hold whose release may never come (a UAC prompt, an admin
        window or a dead hook swallows it) with timed_out, never released:
        the shortcut may still be held, so nothing is typed or sent.

        From the watchdog (held_since set), a hold is re-armed instead while
        the hooks keep seeing input, as live hooks would have seen its
        release, up to MAX_HOLD_SECONDS. M3's own state can't tell us:
        our hook swallows it, so GetAsyncKeyState never sees it."""
        with self._state_lock:
            if held_since is not None:
                if held_since != self._held_since or not self._shortcut_active:
                    return  # let go already, or a newer hold owns the watchdog
                held = time.monotonic() - held_since
                if self._last_input_at > self._watchdog_armed_at and held < MAX_HOLD_SECONDS:
                    self._arm_watchdog_locked(min(WATCHDOG_SECONDS, MAX_HOLD_SECONDS - held))
                    return
            was_active = self._shortcut_active
            self._pressed_tokens.clear()
            self._shortcut_active = False
            self._cancel_watchdog_locked()

        if not was_active:
            return

        self._log("Hold shortcut timed out")
        self.status.emit("Shortcut timed out. Recording stopped automatically.")
        self.timed_out.emit()

    def _required_tokens_for(self, shortcut: ParsedShortcut) -> frozenset[str]:
        values = set(shortcut.modifiers)
        if shortcut.trigger_token:
            values.add(shortcut.trigger_token)
        return frozenset(values)

    def _is_combo_active_locked(self) -> bool:
        return bool(self._required_tokens) and self._required_tokens.issubset(
            self._pressed_tokens
        )

    def _forget_released_modifiers_locked(self, token: str) -> None:
        """Drop modifiers Windows says are up. Their key-up never reached the
        hook (Ctrl+Alt+Del, a UAC prompt), so a plain Q started dictation."""
        for held in self._pressed_tokens & MODIFIER_VKS.keys() - {token}:
            if not _modifier_down(held):
                self._pressed_tokens.discard(held)

    def _arm_watchdog_locked(self, seconds: float) -> None:
        self._cancel_watchdog_locked()
        self._watchdog_armed_at = time.monotonic()
        self._watchdog = threading.Timer(seconds, self._force_release, args=(self._held_since,))
        self._watchdog.daemon = True
        self._watchdog.start()

    def _cancel_watchdog_locked(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _capture_foreground_window(self) -> int | None:
        if win32gui is None:
            return None
        try:
            hwnd = int(win32gui.GetForegroundWindow())
        except Exception:
            return None
        return hwnd or None

    def _canonical_token(self, key: object) -> str | None:
        token = PYNPUT_KEY_TOKENS.get(key)
        if token is not None:
            return token
        vk = getattr(key, "vk", None)
        # The virtual-key code comes first: with Ctrl held, pynput reports
        # control characters ('\x11' for Q) instead of the letter.
        if isinstance(vk, int):
            if 0x70 <= vk <= 0x7B:
                return f"f{vk - 0x6F}"
            if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
                return chr(vk).lower()
            for name, value in KEY_NAME_TO_VK.items():
                if value == vk:
                    return TRIGGER_TOKEN_ALIASES.get(name, name)
        char = getattr(key, "char", None)
        if isinstance(char, str) and char.isprintable():
            return CHAR_TRIGGER_ALIASES.get(char, char.lower())
        return None

    def _log(self, message: str) -> None:
        self.logged.emit(f"hotkey: {message}")
