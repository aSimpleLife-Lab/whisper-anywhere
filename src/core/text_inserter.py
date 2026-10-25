from __future__ import annotations

import ctypes
import threading
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
CLIPBOARD_RESTORE_DELAY = 0.6
# GDI handles and owner-drawn formats; Windows rebuilds the bitmap ones from CF_DIB.
HANDLE_FORMATS = {2, 3, 9, 14, 0x80, 0x81, 0x82, 0x83, 0x8E}
CLIPBOARD_RETRY_SLEEP = 0.01
CLIPBOARD_RETRIES = 3
# Transcription can now finish before the user lets go of the shortcut's
# modifiers; pasting then would send Ctrl+Alt+V instead of Ctrl+V.
MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, LWin, RWin
MODIFIER_WAIT_SECONDS = 1.5
MODIFIER_POLL_SLEEP = 0.01
# Games read the keyboard once per frame, so a key pressed and released within
# the same millisecond never registers. Hold each key for a couple of frames.
KEY_SETTLE_SLEEP = 0.025
KEY_HOLD_SLEEP = 0.045
TYPE_CHAR_SLEEP = 0.002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
QUNS_BUSY = 2  # a full-screen app (or presentation mode) is in front
QUNS_RUNNING_D3D_FULL_SCREEN = 3
# Registered formats that keep dictated text out of Win+V history, the cloud
# clipboard and clipboard monitors.
PRIVATE_CLIPBOARD_FORMATS = {
    "ExcludeClipboardContentFromMonitorProcessing": b"\x00",
    "CanIncludeInClipboardHistory": b"\x00\x00\x00\x00",
    "CanUploadToCloudClipboard": b"\x00\x00\x00\x00",
}

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
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
shell32 = ctypes.windll.shell32


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


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; without it INPUT is too small and
    # SendInput rejects every event on 64-bit Windows.
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def foreground_is_fullscreen() -> bool:
    """True while a full-screen app, usually a game, is in front."""
    state = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except Exception:
        return False
    return state.value in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN)


@dataclass(frozen=True)
class TextTarget:
    window_hwnd: int
    focus_hwnd: int | None = None


class TextInserter:
    def __init__(self) -> None:
        self._keyboard = Controller() if Controller is not None else None
        self._restore_lock = threading.Lock()
        self._pending_restore: threading.Timer | None = None
        self._saved_clipboard: dict[int, Any] | None = None

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

        # Only a target that lost focus needs restoring and time to settle;
        # usually the user never left it.
        if target is not None and int(user32.GetForegroundWindow() or 0) != target.window_hwnd:
            self._restore_target(target)
            delay_ms = int(settings.get("typing_delay_ms", 80) or 0)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)

        if foreground_is_fullscreen():
            # Game chat boxes often ignore Ctrl+V or have no paste at all, but
            # every text box accepts typed characters. Nothing touches the
            # clipboard on this path.
            self._type_text(prepared)
        else:
            self._paste_with_clipboard(
                prepared, bool(settings.get("restore_clipboard", True))
            )
        if bool(settings.get("press_enter_after_text", False)):
            # A pasted newline is just a line break in chat apps; a real Enter
            # key sends the message.
            self._tap_enter()

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

        return value

    def _paste_with_clipboard(self, text: str, restore_clipboard: bool) -> None:
        with self._restore_lock:
            pending, self._pending_restore = self._pending_restore, None
            if pending is not None:
                pending.cancel()  # keep the snapshot taken before that earlier paste
            else:
                self._saved_clipboard = None
            if not restore_clipboard:
                self._saved_clipboard = None
            elif self._saved_clipboard is None:
                self._saved_clipboard = self._snapshot_clipboard()
        self._set_clipboard_text(text, private=True)
        time.sleep(CLIPBOARD_PROPAGATE_SLEEP)
        self._wait_for_modifiers_released()
        self._send_ctrl_v()
        if self._saved_clipboard is None:
            return
        # Apps read the clipboard whenever they get round to the paste, so the
        # old contents come back later, on a timer, not straight away.
        timer = threading.Timer(CLIPBOARD_RESTORE_DELAY, self._restore_clipboard, args=(text,))
        timer.daemon = True
        with self._restore_lock:
            self._pending_restore = timer
        timer.start()

    def _restore_clipboard(self, pasted: str) -> None:
        with self._restore_lock:
            if self._pending_restore is not threading.current_thread():
                return  # a newer paste took over
            self._pending_restore = None
            saved, self._saved_clipboard = self._saved_clipboard, None
        # Leave it alone if the user copied something new meanwhile.
        if saved is None or self._read_clipboard_text() != pasted:
            return
        opened = False
        try:
            self._open_clipboard_with_retry()
            opened = True
            win32clipboard.EmptyClipboard()
            for fmt, data in saved.items():
                try:
                    win32clipboard.SetClipboardData(fmt, data)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass

    def _snapshot_clipboard(self) -> dict[int, Any] | None:
        """Every clipboard format whose data is plain memory: text, rich text, images (DIB)."""
        if win32clipboard is None:
            return None
        saved: dict[int, Any] = {}
        opened = False
        try:
            self._open_clipboard_with_retry()
            opened = True
            fmt = win32clipboard.EnumClipboardFormats(0)
            while fmt:
                if fmt not in HANDLE_FORMATS:
                    try:
                        data = win32clipboard.GetClipboardData(fmt)
                        if isinstance(data, (bytes, str)):
                            saved[fmt] = data
                    except Exception:
                        pass
                fmt = win32clipboard.EnumClipboardFormats(fmt)
        except Exception:
            return saved or None
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        return saved

    def _wait_for_modifiers_released(self) -> None:
        deadline = time.monotonic() + MODIFIER_WAIT_SECONDS
        while time.monotonic() < deadline:
            if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in MODIFIER_VKS):
                return
            time.sleep(MODIFIER_POLL_SLEEP)

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

    def _set_clipboard_text(self, text: str, private: bool = False) -> None:
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
            register = getattr(win32clipboard, "RegisterClipboardFormat", None)
            if private and register is not None:
                for name, value in PRIVATE_CLIPBOARD_FORMATS.items():
                    win32clipboard.SetClipboardData(register(name), value)
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
                time.sleep(KEY_SETTLE_SLEEP)
                self._keyboard.press("v")
                time.sleep(KEY_HOLD_SLEEP)
                self._keyboard.release("v")
                time.sleep(KEY_SETTLE_SLEEP)
        except Exception as exc:
            raise TextInsertionError(
                "Windows blocked clipboard paste into the focused app. Try running Whisper Anywhere at the same permission level as the target app."
            ) from exc

    def _tap_enter(self) -> None:
        if self._keyboard is None or Key is None:
            return
        try:
            self._keyboard.press(Key.enter)
            time.sleep(KEY_HOLD_SLEEP)
            self._keyboard.release(Key.enter)
        except Exception as exc:
            raise TextInsertionError(
                "The text was pasted, but Windows blocked the Enter key."
            ) from exc

    def _type_text(self, text: str) -> None:
        self._wait_for_modifiers_released()
        data = text.encode("utf-16-le")
        # One key press per UTF-16 unit; an emoji goes out as its two halves.
        for index in range(0, len(data), 2):
            self._send_unicode_unit(int.from_bytes(data[index : index + 2], "little"))
            time.sleep(TYPE_CHAR_SLEEP)

    def _send_unicode_unit(self, unit: int) -> None:
        # Unicode key events arrive as typed characters (VK_PACKET), so games
        # do not read them as movement or hotkeys.
        events = (INPUT * 2)(
            INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0))),
            INPUT(
                INPUT_KEYBOARD,
                _INPUTUNION(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)),
            ),
        )
        if user32.SendInput(2, events, ctypes.sizeof(INPUT)) != 2:
            raise TextInsertionError(
                "Windows blocked typing into the focused app. If it runs as administrator, run Whisper Anywhere as administrator too."
            )

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
