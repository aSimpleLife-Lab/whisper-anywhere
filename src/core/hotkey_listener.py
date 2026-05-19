from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

try:
    import win32gui
except Exception:  # pragma: no cover - Windows-only dependency
    win32gui = None

try:
    from pynput import keyboard as pynput_keyboard
except Exception:  # pragma: no cover - dependency missing at runtime
    pynput_keyboard = None

WATCHDOG_SECONDS = 60.0
DEFAULT_START_SOUND_PATH = ""
DEFAULT_STOP_SOUND_PATH = ""

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
        parts = [DISPLAY_MODIFIER[name] for name in ("ctrl", "shift", "alt", "win") if name in self.modifiers]
        if self.trigger_name:
            parts.append(self.trigger_name)
        return "+".join(parts)

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
    parts = [part.strip() for part in shortcut_text.replace("-", "+").split("+") if part.strip()]
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

    return ParsedShortcut(frozenset(modifiers), trigger_vk, trigger_name, trigger_token)


def shortcut_warning(shortcut_text: str) -> str:
    try:
        parsed = parse_shortcut(shortcut_text)
        display = parsed.display.lower().replace(" ", "")
    except ValueError as exc:
        return str(exc)

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
        "ctrl+shift+esc": "Ctrl+Shift+Esc opens Task Manager.",
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
    if not parsed.modifiers and parsed.trigger_vk is not None:
        return "Single-key shortcuts can trigger while typing. Use a modifier if it gets in the way."
    if parsed.trigger_vk is None:
        return "Modifier-only shortcuts are unreliable. Ctrl+Alt+Q is recommended."
    if parsed.trigger_token in {"esc", "tab", "enter", "backspace", "delete", "space"} and len(parsed.modifiers) < 2:
        return "This key is used heavily by Windows and apps. Add two modifiers or use Ctrl+Alt+Q."
    if parsed.trigger_token and parsed.trigger_token.startswith("f") and parsed.trigger_token in {"f1", "f5", "f11", "f12"}:
        return "This function key is commonly used by Windows, browsers, or developer tools."
    if "win" in parsed.modifiers:
        return "Windows-key shortcuts can be intercepted by the shell on some systems. Ctrl+Alt+Q is the safest default."
    if parsed.modifiers == frozenset({"ctrl"}) or parsed.modifiers == frozenset({"alt"}):
        return "Single-modifier shortcuts often conflict with app menus and editor/browser shortcuts. Ctrl+Alt+Q is recommended."
    return ""


def _default_hotkey_log_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Whisper Anywhere" / "hotkey.log"
    return Path.home() / "AppData" / "Roaming" / "Whisper Anywhere" / "hotkey.log"


class HotkeyListener(QObject):
    pressed = Signal()
    released = Signal(object)
    cancelled = Signal()
    error = Signal(str)
    feedback = Signal(bool)
    status = Signal(str)

    def __init__(
        self,
        shortcut: str = "Ctrl+Alt+Q",
        mode: str = "hold",
        log_path: str | Path | None = None,
        start_sound_path: str | Path | None = DEFAULT_START_SOUND_PATH,
        stop_sound_path: str | Path | None = DEFAULT_STOP_SOUND_PATH,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.shortcut = parse_shortcut(shortcut)
        self.mode = mode if mode in ("hold", "toggle") else "hold"
        self.log_path = Path(log_path) if log_path else _default_hotkey_log_path()
        self.start_sound_path = Path(start_sound_path) if start_sound_path else None
        self.stop_sound_path = Path(stop_sound_path) if stop_sound_path else None
        self._required_tokens = self._required_tokens_for(self.shortcut)
        self._pressed_tokens: set[str] = set()
        self._shortcut_active = False
        self._toggle_listening = False
        self._running = False
        self._listener: object | None = None
        self._watchdog: threading.Timer | None = None
        self._state_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._log_failed = False

    def start(self) -> None:
        if self._running:
            return
        if pynput_keyboard is None:
            message = "The global shortcut dependency is missing. Reinstall Whisper Anywhere to restore hotkeys."
            self._log(f"Hotkey start failed: {message}")
            self.error.emit(message)
            self.status.emit(message)
            return

        self._log(f"Starting pynput hotkey listener: shortcut={self.shortcut.display}, mode={self.mode}")
        self.status.emit(f"Starting global shortcut listener. Log: {self.log_path}")
        self._running = True
        try:
            listener = pynput_keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            listener.daemon = True
            listener.start()
        except Exception as exc:
            self._running = False
            message = f"Could not start the global shortcut listener. {exc}"
            self._log(message)
            self.error.emit(message)
            self.status.emit(message)
            return

        self._listener = listener
        self._log("pynput keyboard listener started")
        self.status.emit(f"Global shortcut ready: {self.shortcut.display}.")

    def stop(self) -> None:
        if not self._running and self._listener is None:
            return

        self._log("Stopping hotkey listener")
        self.status.emit("Stopping global shortcut listener.")
        self._running = False
        with self._state_lock:
            self._pressed_tokens.clear()
            self._shortcut_active = False
            self._toggle_listening = False
            self._cancel_watchdog_locked()

        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.stop()
            if listener.is_alive():
                listener.join(timeout=2)

        self.status.emit("Global shortcut listener stopped.")

    def update_shortcut(self, shortcut: str, mode: str) -> None:
        parsed = parse_shortcut(shortcut)
        with self._state_lock:
            self.shortcut = parsed
            self.mode = mode if mode in ("hold", "toggle") else "hold"
            self._required_tokens = self._required_tokens_for(parsed)
            self._pressed_tokens.clear()
            self._shortcut_active = False
            self._toggle_listening = False
            self._cancel_watchdog_locked()
        self._log(f"Updated hotkey: shortcut={self.shortcut.display}, mode={self.mode}")
        self.status.emit(f"Shortcut active: {self.shortcut.display} ({self.mode}).")

    def _on_press(self, key: object) -> None:
        if not self._running:
            return

        token = self._canonical_token(key)
        if token is None:
            return

        if token == "esc" and self._cancel_active_recording():
            return

        emit_pressed = False
        emit_toggle_on = False
        emit_toggle_off = False
        with self._state_lock:
            was_combo_active = self._shortcut_active
            self._pressed_tokens.add(token)
            is_combo_active = self._is_combo_active_locked()
            self._shortcut_active = is_combo_active
            if self.mode == "hold":
                if is_combo_active and not was_combo_active:
                    self._arm_watchdog_locked()
                    emit_pressed = True
            elif is_combo_active and not was_combo_active:
                self._toggle_listening = not self._toggle_listening
                if self._toggle_listening:
                    emit_toggle_on = True
                else:
                    emit_toggle_off = True

        if emit_pressed:
            self._log("Hold shortcut pressed")
            self.status.emit(f"Shortcut pressed: {self.shortcut.display}.")
            self._play_feedback(True)
            self.pressed.emit()
        elif emit_toggle_on:
            self._log("Toggle shortcut started listening")
            self.status.emit(f"Shortcut toggled on: {self.shortcut.display}.")
            self._play_feedback(True)
            self.pressed.emit()
        elif emit_toggle_off:
            self._log("Toggle shortcut stopped listening")
            self.status.emit(f"Shortcut toggled off: {self.shortcut.display}.")
            self._play_feedback(False)
            self.released.emit(None)

    def _on_release(self, key: object) -> None:
        if not self._running:
            return

        token = self._canonical_token(key)
        if token is None:
            return

        emit_released = False
        with self._state_lock:
            was_combo_active = self._shortcut_active
            was_in_combo = token in self._required_tokens
            self._pressed_tokens.discard(token)
            is_combo_active = self._is_combo_active_locked()
            self._shortcut_active = is_combo_active
            if self.mode == "hold" and was_combo_active and was_in_combo and not is_combo_active:
                self._cancel_watchdog_locked()
                emit_released = True

        if emit_released:
            hwnd = self._capture_foreground_window()
            self._log(f"Hold shortcut released target={hwnd}")
            self.status.emit(f"Shortcut released: {self.shortcut.display}.")
            self._play_feedback(False)
            self.released.emit(hwnd)

    def _cancel_active_recording(self) -> bool:
        with self._state_lock:
            if not (self._shortcut_active or self._toggle_listening):
                return False
            self._pressed_tokens.clear()
            self._shortcut_active = False
            self._toggle_listening = False
            self._cancel_watchdog_locked()

        self._log("Cancel key pressed: Esc")
        self.status.emit("Recording cancelled.")
        self.cancelled.emit()
        return True

    def _force_release(self) -> None:
        with self._state_lock:
            was_active = self._shortcut_active
            self._pressed_tokens.clear()
            self._shortcut_active = False
            self._cancel_watchdog_locked()

        if not was_active:
            return

        self._log("Hold shortcut watchdog forced release")
        self.status.emit("Shortcut timed out. Recording stopped automatically.")
        self._play_feedback(False)
        self.released.emit(None)

    def _required_tokens_for(self, shortcut: ParsedShortcut) -> frozenset[str]:
        values = set(shortcut.modifiers)
        if shortcut.trigger_token:
            values.add(shortcut.trigger_token)
        return frozenset(values)

    def _is_combo_active_locked(self) -> bool:
        return bool(self._required_tokens) and self._required_tokens.issubset(self._pressed_tokens)

    def _arm_watchdog_locked(self) -> None:
        self._cancel_watchdog_locked()
        self._watchdog = threading.Timer(WATCHDOG_SECONDS, self._force_release)
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
        char = getattr(key, "char", None)
        if isinstance(char, str) and char:
            return CHAR_TRIGGER_ALIASES.get(char, char.lower())
        vk = getattr(key, "vk", None)
        if isinstance(vk, int):
            if 0x70 <= vk <= 0x7B:
                return f"f{vk - 0x6F}"
            if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
                return chr(vk).lower()
            for name, value in KEY_NAME_TO_VK.items():
                if value == vk:
                    return TRIGGER_TOKEN_ALIASES.get(name, name)
        return None

    def _play_feedback(self, active: bool) -> None:
        self._log(f"Feedback requested: {'start' if active else 'stop'}")
        self.feedback.emit(active)

    def _log(self, message: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            with self._log_lock:
                with self.log_path.open("a", encoding="utf-8") as file:
                    file.write(f"{timestamp} {message}\n")
        except OSError as exc:
            if not self._log_failed:
                self._log_failed = True
                self.status.emit(f"Hotkey logging is unavailable: {exc}")
