from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
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

try:
    from pynput.keyboard import Controller, Key
except Exception:  # pragma: no cover - dependency missing at runtime
    Controller = None
    Key = None

FOCUS_RESTORE_SLEEP = 0.05
CLIPBOARD_PROPAGATE_SLEEP = 0.05
PASTE_PROPAGATE_SLEEP = 0.05
CLIPBOARD_RETRY_SLEEP = 0.01
CLIPBOARD_RETRIES = 3

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
)
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.c_void_p)
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetFocus.argtypes = (wintypes.HWND,)
user32.SetFocus.restype = wintypes.HWND
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.AttachThreadInput.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class TextInsertionError(RuntimeError):
    pass


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


@dataclass(frozen=True)
class TextTarget:
    window_hwnd: int
    focus_hwnd: int | None = None


class TextInserter:
    def __init__(self) -> None:
        self._keyboard = Controller() if Controller is not None else None

    def get_foreground_window(self) -> TextTarget | None:
        window_hwnd = int(user32.GetForegroundWindow())
        return self.get_window_target(window_hwnd)

    def get_window_target(self, window_hwnd: int | None) -> TextTarget | None:
        if not window_hwnd or not user32.IsWindow(int(window_hwnd)):
            return None
        focus_hwnd = self._get_focused_control(int(window_hwnd))
        if focus_hwnd == int(window_hwnd):
            focus_hwnd = None
        return TextTarget(window_hwnd=int(window_hwnd), focus_hwnd=focus_hwnd)

    def insert_text(
        self, text: str, target: TextTarget | None, settings: dict[str, Any]
    ) -> None:
        prepared = self._prepare_text(text, settings)
        if not prepared:
            return

        delay_ms = int(settings.get("typing_delay_ms", 80) or 0)
        self._restore_target(target)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

        self._paste_with_clipboard(
            prepared, bool(settings.get("restore_clipboard", True))
        )

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

        if bool(settings.get("add_space_after_text", False)) and not value.endswith(
            " "
        ):
            value += " "

        if bool(settings.get("press_enter_after_text", False)):
            value += "\n"

        return value

    def _paste_with_clipboard(self, text: str, restore_clipboard: bool) -> None:
        previous_text = self._read_clipboard_text() if restore_clipboard else None
        self._set_clipboard_text(text)
        time.sleep(CLIPBOARD_PROPAGATE_SLEEP)
        self._send_ctrl_v()
        time.sleep(PASTE_PROPAGATE_SLEEP)
        if restore_clipboard and previous_text is not None:
            self._set_clipboard_text(previous_text)

    def _read_clipboard_text(self) -> str | None:
        if win32clipboard is None or win32con is None:
            return None
        opened = False
        try:
            self._open_clipboard_with_retry()
            opened = True
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
        except (TypeError, OSError):
            return None
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
        if win32clipboard is None or win32con is None:
            raise TextInsertionError(
                "Clipboard support is unavailable. Reinstall Whisper Anywhere with pywin32 included."
            )
        opened = False
        try:
            self._open_clipboard_with_retry()
            opened = True
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        except Exception as exc:
            raise TextInsertionError(
                "Could not write to the Windows clipboard. Close clipboard manager apps and try again."
            ) from exc
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass

    def _open_clipboard_with_retry(self) -> None:
        if win32clipboard is None:
            raise TextInsertionError(
                "Clipboard support is unavailable. Reinstall Whisper Anywhere with pywin32 included."
            )
        last_error: Exception | None = None
        for attempt in range(CLIPBOARD_RETRIES):
            try:
                win32clipboard.OpenClipboard()
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 < CLIPBOARD_RETRIES:
                    time.sleep(CLIPBOARD_RETRY_SLEEP)
        if last_error is not None:
            raise last_error

    def _send_ctrl_v(self) -> None:
        if self._keyboard is None or Key is None:
            raise TextInsertionError(
                "Keyboard control is unavailable. Reinstall Whisper Anywhere with pynput included."
            )
        try:
            with self._keyboard.pressed(Key.ctrl):
                self._keyboard.press("v")
                self._keyboard.release("v")
        except Exception as exc:
            raise TextInsertionError(
                "Windows blocked clipboard paste into the focused app. Try running Whisper Anywhere at the same permission level as the target app."
            ) from exc

    def _get_focused_control(self, window_hwnd: int) -> int | None:
        thread_id = user32.GetWindowThreadProcessId(window_hwnd, None)
        if not thread_id:
            return None
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return None
        return int(info.hwndFocus) if info.hwndFocus else None

    def _restore_target(self, target: TextTarget | None) -> None:
        if target is None or not user32.IsWindow(target.window_hwnd):
            return
        try:
            if win32gui is not None:
                win32gui.BringWindowToTop(target.window_hwnd)
            user32.SetForegroundWindow(target.window_hwnd)
            time.sleep(FOCUS_RESTORE_SLEEP)
        except Exception:
            pass
        if target.focus_hwnd and user32.IsWindow(target.focus_hwnd):
            self._restore_focus(target.focus_hwnd)

    def _restore_focus(self, focus_hwnd: int) -> None:
        target_thread = user32.GetWindowThreadProcessId(focus_hwnd, None)
        current_thread = kernel32.GetCurrentThreadId()
        attached = False
        try:
            if target_thread and target_thread != current_thread:
                attached = bool(
                    user32.AttachThreadInput(current_thread, target_thread, True)
                )
            user32.SetFocus(focus_hwnd)
        except Exception:
            pass
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, target_thread, False)
