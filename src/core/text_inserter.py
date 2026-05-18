from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

try:
    import win32clipboard
    import win32con
    import win32gui
except Exception:  # pragma: no cover - Windows-only dependency
    win32clipboard = None
    win32con = None
    win32gui = None

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_V = 0x56

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


user32 = ctypes.windll.user32
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT


class TextInsertionError(RuntimeError):
    pass


class TextInserter:
    def get_foreground_window(self) -> int:
        if win32gui is None:
            return 0
        try:
            return int(win32gui.GetForegroundWindow())
        except Exception:
            return 0

    def insert_text(self, text: str, target_hwnd: int | None, settings: dict[str, Any]) -> None:
        prepared = self._prepare_text(text, settings)
        if not prepared:
            return

        delay_ms = int(settings.get("typing_delay_ms", 80) or 0)
        if target_hwnd and win32gui is not None:
            try:
                win32gui.SetForegroundWindow(target_hwnd)
            except Exception:
                pass

        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

        method = str(settings.get("insert_method", "clipboard_paste"))
        if method == "simulated_keystrokes":
            self._type_unicode(prepared)
            return

        self._paste_with_clipboard(prepared, bool(settings.get("restore_clipboard", True)))

    def _prepare_text(self, text: str, settings: dict[str, Any]) -> str:
        value = " ".join(text.split()).strip()
        if not value:
            return ""

        if bool(settings.get("auto_capitalization", False)):
            for index, char in enumerate(value):
                if char.isalpha():
                    value = value[:index] + char.upper() + value[index + 1 :]
                    break

        if bool(settings.get("auto_punctuation", False)) and value[-1] not in ".!?":
            value += "."

        if bool(settings.get("add_space_after_text", False)) and not value.endswith(" "):
            value += " "

        if bool(settings.get("press_enter_after_text", False)):
            value += "\n"

        return value

    def _paste_with_clipboard(self, text: str, restore_clipboard: bool) -> None:
        previous_text = self._read_clipboard_text() if restore_clipboard else None
        self._set_clipboard_text(text)
        time.sleep(0.05)
        self._send_ctrl_v()
        time.sleep(0.12)
        if restore_clipboard and previous_text is not None:
            self._set_clipboard_text(previous_text)

    def _read_clipboard_text(self) -> str | None:
        if win32clipboard is None or win32con is None:
            return None
        opened = False
        try:
            win32clipboard.OpenClipboard()
            opened = True
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
        except Exception:
            return None
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        return None

    def _set_clipboard_text(self, text: str) -> None:
        if win32clipboard is None:
            raise TextInsertionError("Clipboard support is unavailable. Reinstall Whisper Anywhere with pywin32 included.")
        opened = False
        try:
            win32clipboard.OpenClipboard()
            opened = True
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        except Exception as exc:
            raise TextInsertionError("Could not write to the Windows clipboard. Close clipboard manager apps and try again.") from exc
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass

    def _send_ctrl_v(self) -> None:
        inputs = (INPUT * 4)(
            self._key_input(VK_CONTROL, 0),
            self._key_input(VK_V, 0),
            self._key_input(VK_V, KEYEVENTF_KEYUP),
            self._key_input(VK_CONTROL, KEYEVENTF_KEYUP),
        )
        sent = user32.SendInput(4, inputs, ctypes.sizeof(INPUT))
        if sent != 4:
            raise TextInsertionError("Windows blocked paste input. Try running Whisper Anywhere at the same permission level as the target app.")

    def _type_unicode(self, text: str) -> None:
        for char in text:
            code = ord(char)
            inputs = (INPUT * 2)(
                self._unicode_input(code, 0),
                self._unicode_input(code, KEYEVENTF_KEYUP),
            )
            sent = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
            if sent != 2:
                raise TextInsertionError("Windows blocked simulated typing into the focused app.")
            time.sleep(0.002)

    def _key_input(self, virtual_key: int, flags: int) -> INPUT:
        return INPUT(
            type=INPUT_KEYBOARD,
            union=INPUT_UNION(ki=KEYBDINPUT(virtual_key, 0, flags, 0, 0)),
        )

    def _unicode_input(self, codepoint: int, flags: int) -> INPUT:
        return INPUT(
            type=INPUT_KEYBOARD,
            union=INPUT_UNION(ki=KEYBDINPUT(0, codepoint, KEYEVENTF_UNICODE | flags, 0, 0)),
        )
