from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

VK_ESCAPE = 0x1B
VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_LWIN = 0x5B
VK_RWIN = 0x5C

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
LRESULT = getattr(wintypes, "LRESULT", ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long)
HHOOK = getattr(wintypes, "HHOOK", wintypes.HANDLE)
HINSTANCE = getattr(wintypes, "HINSTANCE", wintypes.HANDLE)

MODIFIER_VKS = {
    "ctrl": {VK_CONTROL, VK_LCONTROL, VK_RCONTROL},
    "shift": {VK_SHIFT, VK_LSHIFT, VK_RSHIFT},
    "alt": {VK_MENU, VK_LMENU, VK_RMENU},
    "win": {VK_LWIN, VK_RWIN},
}

VK_DISPLAY_NAMES = {
    VK_ESCAPE: "Esc",
    VK_CONTROL: "Ctrl",
    VK_LCONTROL: "Left Ctrl",
    VK_RCONTROL: "Right Ctrl",
    VK_SHIFT: "Shift",
    VK_LSHIFT: "Left Shift",
    VK_RSHIFT: "Right Shift",
    VK_MENU: "Alt",
    VK_LMENU: "Left Alt",
    VK_RMENU: "Right Alt",
    VK_LWIN: "Left Win",
    VK_RWIN: "Right Win",
    0x20: "Space",
}

KEY_NAME_TO_VK = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": VK_ESCAPE,
    "escape": VK_ESCAPE,
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

DISPLAY_MODIFIER = {
    "ctrl": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "win": "Win",
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, LowLevelKeyboardProc, HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = HHOOK
user32.CallNextHookEx.argtypes = (HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = (HHOOK,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.restype = LRESULT
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = HINSTANCE


@dataclass(frozen=True)
class ParsedShortcut:
    modifiers: frozenset[str]
    trigger_vk: int | None
    trigger_name: str | None

    @property
    def display(self) -> str:
        parts = [DISPLAY_MODIFIER[name] for name in ("ctrl", "shift", "alt", "win") if name in self.modifiers]
        if self.trigger_name:
            parts.append(self.trigger_name)
        return "+".join(parts)

    def involved_vks(self) -> set[int]:
        values: set[int] = set()
        for modifier in self.modifiers:
            values.update(MODIFIER_VKS[modifier])
        if self.trigger_vk is not None:
            values.add(self.trigger_vk)
        return values


def parse_shortcut(shortcut_text: str) -> ParsedShortcut:
    parts = [part.strip() for part in shortcut_text.replace("-", "+").split("+") if part.strip()]
    if not parts:
        raise ValueError("Enter a shortcut such as Ctrl+Win or Ctrl+Win+Space.")

    modifiers: set[str] = set()
    trigger_vk: int | None = None
    trigger_name: str | None = None

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
            if part not in KEY_NAME_TO_VK:
                raise ValueError(f"Unsupported shortcut key: {raw_part}")
            trigger_vk = KEY_NAME_TO_VK[part]
            trigger_name = raw_part.upper() if len(raw_part) == 1 else raw_part.title().replace("Pageup", "PageUp").replace("Pagedown", "PageDown")

    if not modifiers and trigger_vk is None:
        raise ValueError("Shortcut must include Ctrl, Win, Alt, or Shift.")

    return ParsedShortcut(frozenset(modifiers), trigger_vk, trigger_name)


def shortcut_warning(shortcut_text: str) -> str:
    try:
        display = parse_shortcut(shortcut_text).display.lower().replace(" ", "")
    except ValueError as exc:
        return str(exc)

    common = {
        "ctrl+c": "Ctrl+C is copy in most apps.",
        "ctrl+v": "Ctrl+V is paste in most apps.",
        "ctrl+x": "Ctrl+X is cut in most apps.",
        "ctrl+z": "Ctrl+Z is undo in most apps.",
        "alt+tab": "Alt+Tab switches apps in Windows.",
        "win+l": "Win+L locks Windows.",
        "win+d": "Win+D shows the desktop.",
        "win+r": "Win+R opens Run.",
        "win+e": "Win+E opens File Explorer.",
    }
    if display in common:
        return common[display]
    if display == "ctrl+win":
        return "Modifier-only Ctrl+Win can be unreliable on Windows. Ctrl+Win+Space is recommended."
    return ""


def _default_hotkey_log_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Whisper Anywhere" / "hotkey.log"
    return Path.home() / "AppData" / "Roaming" / "Whisper Anywhere" / "hotkey.log"


class HotkeyListener(QObject):
    pressed = Signal()
    released = Signal()
    cancelled = Signal()
    error = Signal(str)
    status = Signal(str)

    def __init__(
        self,
        shortcut: str = "Ctrl+Win+Space",
        mode: str = "hold",
        log_path: str | Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.shortcut = parse_shortcut(shortcut)
        self.mode = mode if mode in ("hold", "toggle") else "hold"
        self.log_path = Path(log_path) if log_path else _default_hotkey_log_path()
        self._pressed_vks: set[int] = set()
        self._shortcut_active = False
        self._toggle_listening = False
        self._running = False
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook_handle = None
        self._suppress_win_until_up = False
        self._log_lock = threading.Lock()
        self._log_failed = False
        self._callback = LowLevelKeyboardProc(self._keyboard_proc)

    def start(self) -> None:
        if self._running:
            return
        self._log(f"Starting hotkey listener: shortcut={self.shortcut.display}, mode={self.mode}")
        self.status.emit(f"Starting global shortcut listener. Log: {self.log_path}")
        self._running = True
        self._thread = threading.Thread(target=self._run_message_loop, name="WhisperAnywhereHotkey", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._log("Stopping hotkey listener")
        self.status.emit("Stopping global shortcut listener.")
        self._running = False
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def update_shortcut(self, shortcut: str, mode: str) -> None:
        self.shortcut = parse_shortcut(shortcut)
        self.mode = mode if mode in ("hold", "toggle") else "hold"
        self._pressed_vks.clear()
        self._shortcut_active = False
        self._toggle_listening = False
        self._suppress_win_until_up = False
        self._log(f"Updated hotkey: shortcut={self.shortcut.display}, mode={self.mode}")
        self.status.emit(f"Shortcut active: {self.shortcut.display} ({self.mode}).")

    def _run_message_loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        self._log(f"Installing WH_KEYBOARD_LL hook on thread={self._thread_id}")
        self._hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._callback, kernel32.GetModuleHandleW(None), 0)
        if not self._hook_handle:
            message = (
                "Could not register the global keyboard hook. "
                f"{self._last_error_message()} Restart the app or check security software settings."
            )
            self._log(f"Hook install failed: {message}")
            self.error.emit(message)
            self.status.emit(message)
            self._running = False
            return
        self._log(f"Keyboard hook installed: handle={int(self._hook_handle)}")
        self.status.emit(f"Global shortcut ready: {self.shortcut.display}.")

        msg = wintypes.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook_handle:
            user32.UnhookWindowsHookEx(self._hook_handle)
            self._log("Keyboard hook uninstalled")
            self._hook_handle = None
        self._thread_id = 0
        self.status.emit("Global shortcut listener stopped.")

    def _keyboard_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code != HC_ACTION:
            return user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)

        event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk_code = int(event.vkCode)
        is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = w_param in (WM_KEYUP, WM_SYSKEYUP)

        if is_down and vk_code == VK_ESCAPE:
            self._log("Cancel key pressed: Esc")
            self.cancelled.emit()
            self._toggle_listening = False
            return user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)

        was_active = self._shortcut_active
        if is_down:
            self._pressed_vks.add(vk_code)
        elif is_up:
            self._pressed_vks.discard(vk_code)

        is_active = self._is_shortcut_pressed()
        self._shortcut_active = is_active
        self._log_key_transition(vk_code, is_down, is_up, was_active, is_active, int(event.flags))

        if self.mode == "hold":
            if is_active and not was_active:
                self._log("Hold shortcut pressed")
                self.status.emit(f"Shortcut pressed: {self.shortcut.display}.")
                self.pressed.emit()
            elif was_active and not is_active:
                self._log("Hold shortcut released")
                self.status.emit(f"Shortcut released: {self.shortcut.display}.")
                self.released.emit()
        else:
            if is_active and not was_active:
                self._toggle_listening = not self._toggle_listening
                if self._toggle_listening:
                    self._log("Toggle shortcut started listening")
                    self.status.emit(f"Shortcut toggled on: {self.shortcut.display}.")
                    self.pressed.emit()
                else:
                    self._log("Toggle shortcut stopped listening")
                    self.status.emit(f"Shortcut toggled off: {self.shortcut.display}.")
                    self.released.emit()

        if self._should_suppress_event(vk_code, is_down, is_up, was_active, is_active):
            return 1
        return user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)

    def _is_shortcut_pressed(self) -> bool:
        for modifier in self.shortcut.modifiers:
            if not self._pressed_vks.intersection(MODIFIER_VKS[modifier]):
                return False
        if self.shortcut.trigger_vk is not None and self.shortcut.trigger_vk not in self._pressed_vks:
            return False
        return True

    def _should_suppress_event(self, vk_code: int, is_down: bool, is_up: bool, was_active: bool, is_active: bool) -> bool:
        if vk_code not in (VK_LWIN, VK_RWIN):
            return False
        if "win" not in self.shortcut.modifiers:
            return False

        ctrl_is_down = bool(self._pressed_vks.intersection(MODIFIER_VKS["ctrl"]))
        if is_down and (ctrl_is_down or was_active or is_active):
            self._suppress_win_until_up = True
            return True
        if is_up and self._suppress_win_until_up:
            self._suppress_win_until_up = False
            return True
        return was_active or is_active

    def _log_key_transition(self, vk_code: int, is_down: bool, is_up: bool, was_active: bool, is_active: bool, flags: int) -> None:
        if not (is_down or is_up):
            return
        if vk_code != VK_ESCAPE and vk_code not in self.shortcut.involved_vks():
            return
        action = "down" if is_down else "up"
        self._log(
            f"key {action}: {self._vk_name(vk_code)} vk={vk_code} flags=0x{flags:08x} "
            f"pressed=[{self._pressed_keys_text()}] active={is_active} was_active={was_active}"
        )

    def _pressed_keys_text(self) -> str:
        names = [self._vk_name(vk_code) for vk_code in sorted(self._pressed_vks)]
        return ", ".join(names)

    def _vk_name(self, vk_code: int) -> str:
        if vk_code in VK_DISPLAY_NAMES:
            return VK_DISPLAY_NAMES[vk_code]
        if 0x30 <= vk_code <= 0x39 or 0x41 <= vk_code <= 0x5A:
            return chr(vk_code)
        return f"VK_{vk_code}"

    def _last_error_message(self) -> str:
        error_code = ctypes.get_last_error()
        if not error_code:
            return "Windows did not provide an error code."
        try:
            return str(ctypes.WinError(error_code))
        except Exception:
            return f"Windows error {error_code}."

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
