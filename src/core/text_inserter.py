from __future__ import annotations

import ctypes
import os
import threading
import time
import unicodedata
from dataclasses import dataclass
from ctypes import wintypes
from typing import Any

# Marks every key we send, so the shortcut hooks, in their own process,
# never take our paste's V or Enter for the shortcut.
from core.hotkey_listener import REPLAY_MARKER

try:
    import win32api
    import win32clipboard
    import win32con
    import win32gui
    import win32process
    import win32security
except Exception:  # pragma: no cover - Windows-only dependency
    win32api = None
    win32clipboard = None
    win32con = None
    win32gui = None
    win32process = None
    win32security = None

CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CLIPBOARD_PROPAGATE_SLEEP = 0.05
# GDI handles and owner-drawn formats; Windows rebuilds the bitmap ones from
# CF_DIB. EMF pictures are GDI handles too, but go through their bits.
HANDLE_FORMATS = {2, 3, 9, 0x80, 0x81, 0x82, 0x83, 0x8E}
CLIPBOARD_RETRY_SLEEP = 0.01
CLIPBOARD_RETRIES = 3
# The snapshot and the restore can wait for a clipboard manager or remote
# desktop that holds the clipboard for a moment, instead of losing it.
CLIPBOARD_PATIENT_RETRIES = 50
# The text goes on the clipboard rendered on request, so Windows tells us when
# the app reads it. Asking the window whether it was busy did not work: a
# Chromium window answers at once while its page reads the paste much later.
# The send key waits for that read; none in READ_WAIT_SECONDS means the app
# did not take the paste.
READ_WAIT_SECONDS = 5.0
# A later read still pastes our text, so the old clipboard waits for it, up to
# READ_CAP_SECONDS, and comes back then anyway.
READ_CAP_SECONDS = 60.0
READ_SETTLE_SECONDS = 0.15  # apps may read another format right after the text
READ_POLL_SECONDS = 0.05
WM_RENDERFORMAT = 0x0305
WM_RENDERALLFORMATS = 0x0306
WM_DESTROYCLIPBOARD = 0x0307
GMEM_MOVEABLE = 0x0002
# Transcription can now finish before the user lets go of the shortcut's
# modifiers; pasting then would send Ctrl+Alt+V instead of Ctrl+V. A Shift held
# to sprint turned Enter into Shift+Enter (a new line in Discord), and Alt into
# Alt+Enter (full screen in games), so a key still down after the wait stops it.
MODIFIER_KEYS = ((0x10, "Shift"), (0x11, "Ctrl"), (0x12, "Alt"), (0x5B, "Win"), (0x5C, "Win"))
MODIFIER_WAIT_SECONDS = 3.0
SEND_KEY_MODIFIER_WAIT_SECONDS = 0.5
MODIFIER_POLL_SLEEP = 0.01
MODIFIER_RELEASE_SETTLE = 0.1  # Alt+Tab switches windows as Alt goes up
# Games read the keyboard once per frame, so a key pressed and released within
# the same millisecond never registers. Hold each key for a couple of frames.
KEY_SETTLE_SLEEP = 0.025
KEY_HOLD_SLEEP = 0.045
TYPE_CHAR_SLEEP = 0.002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
SEND_KEYS = {"enter": ("Enter", (VK_RETURN,)), "ctrl+enter": ("Ctrl+Enter", (VK_CONTROL, VK_RETURN))}
QUNS_BUSY = 2  # a full-screen app (or presentation mode) is in front
QUNS_RUNNING_D3D_FULL_SCREEN = 3
# The taskbar, tray and desktop take the focus when clicked, but have no text box.
SHELL_WINDOW_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow", "Progman", "WorkerW"}
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SWITCHED = "You switched windows, so the text was not typed. Copy it from Last transcription."
# Registered formats that keep dictated text out of Win+V history, the cloud
# clipboard and clipboard monitors.
PRIVATE_CLIPBOARD_FORMATS = {
    "ExcludeClipboardContentFromMonitorProcessing": b"\x00",
    "CanIncludeInClipboardHistory": b"\x00\x00\x00\x00",
    "CanUploadToCloudClipboard": b"\x00\x00\x00\x00",
}

user32 = ctypes.windll.user32
user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32 = ctypes.windll.kernel32
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
kernel32.GlobalSize.argtypes = (ctypes.c_void_p,)
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
gdi32 = ctypes.windll.gdi32
gdi32.GetEnhMetaFileBits.argtypes = (wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p)
gdi32.SetEnhMetaFileBits.argtypes = (wintypes.UINT, ctypes.c_char_p)
gdi32.SetEnhMetaFileBits.restype = wintypes.HANDLE
gdi32.DeleteEnhMetaFile.argtypes = (wintypes.HANDLE,)
shell32 = ctypes.windll.shell32


class TextInsertionError(RuntimeError):
    """Shown to the user as it is; the text was not delivered."""


class InsertionCancelled(TextInsertionError):
    """The cancel event was set."""


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
    title: str = ""  # window title when captured
    process_id: int = 0
    process_name: str = ""  # lower-case exe file name, e.g. "discord.exe"; "" if unknown


def _window_pid(hwnd: int) -> int:
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:
        return 0


def _window_class(hwnd: int) -> str:
    try:
        return win32gui.GetClassName(hwnd)
    except Exception:
        return ""


def _window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return ""


def _window_processes(hwnd: int) -> frozenset[int]:
    """The processes behind a window and its child windows. Any of them may
    read the paste: a UWP app runs inside the frame host's window, and a
    WebView2 app's page belongs to Edge's process."""
    pids = {_window_pid(hwnd)}
    try:
        win32gui.EnumChildWindows(hwnd, lambda child, _: pids.add(_window_pid(child)), None)
    except Exception:
        pass
    pids.discard(0)
    return frozenset(pids)


def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else None
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return ""
        return os.path.basename(path.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def _integrity_level(pid: int) -> int | None:
    """0x2000 for a normal app, 0x3000 for one run as administrator; None
    when Windows won't say."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else None
    if not handle:
        return None
    try:
        token = win32security.OpenProcessToken(handle, win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)[0]
        return int(sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1))
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _title_key(title: str) -> str:
    # ponytail: letters only, so unread counters ("(3) "), bullets and edit
    # marks are no chat change, but chats whose names differ only in digits or
    # punctuation (#team-1, #team-2) look the same; ask the app if that bites.
    return "".join(char for char in title.casefold() if char.isalpha())


def _send_key_setting(settings: dict[str, Any]) -> tuple[str, tuple[int, ...]] | None:
    key = settings.get("send_key")
    if key is None:
        key = "enter" if settings.get("press_enter_after_text") else "none"
    return SEND_KEYS.get(str(key))


def _check_cancel(cancel: threading.Event | None, message: str) -> None:
    if cancel is not None and cancel.is_set():
        raise InsertionCancelled(message)


def _send_key_event(vk: int, flags: int) -> bool:
    # A real virtual key with its scan code: pynput sent "v" as a typed
    # character on layouts without a Latin V, and never said when Windows
    # refused a key, as it does for an app run as administrator.
    key = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, REPLAY_MARKER)
    event = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=key))
    return user32.SendInput(1, (INPUT * 1)(event), ctypes.sizeof(INPUT)) == 1


def _handle_bytes(fmt: int, handle: int) -> bytes | None:
    """A clipboard handle's data as bytes: an EMF picture's bits, or the
    global memory of any other format, exactly as it is."""
    if not handle:
        return None
    if fmt == CF_ENHMETAFILE:
        size = gdi32.GetEnhMetaFileBits(handle, 0, None)
        bits = ctypes.create_string_buffer(size)
        return bits.raw if size and gdi32.GetEnhMetaFileBits(handle, size, bits) == size else None
    size = kernel32.GlobalSize(handle)
    pointer = kernel32.GlobalLock(handle) if size else None
    if not pointer:
        return None
    try:
        return ctypes.string_at(pointer, size)
    finally:
        kernel32.GlobalUnlock(handle)


def _new_handle(fmt: int, data: bytes) -> int:
    """Fresh memory holding the data, for SetClipboardData; 0 if none."""
    if fmt == CF_ENHMETAFILE:
        return gdi32.SetEnhMetaFileBits(len(data), data) or 0
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    pointer = kernel32.GlobalLock(handle) if handle else None
    if not pointer:
        if handle:
            kernel32.GlobalFree(handle)
        return 0
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def _free_handle(fmt: int, handle: int) -> None:
    (gdi32.DeleteEnhMetaFile if fmt == CF_ENHMETAFILE else kernel32.GlobalFree)(handle)


def _offer_on_request(fmt: int) -> None:
    # pywin32 reads the NULL that SetClipboardData returns for a format
    # rendered on request as a failure, so this one goes through ctypes.
    user32.SetClipboardData(fmt, None)


def _mark_private() -> None:
    for name, value in PRIVATE_CLIPBOARD_FORMATS.items():
        win32clipboard.SetClipboardData(win32clipboard.RegisterClipboardFormat(name), value)


class _Offer:
    """One paste's text, on the clipboard rendered on request, and what
    became of it."""

    def __init__(self, text: str, pids: frozenset[int]) -> None:
        self.text = text
        self.pids = pids  # the target's processes: only their read is the paste
        self.started = time.monotonic()
        self.pasted = False  # Ctrl+V went out; a read before it is no paste
        self.read = threading.Event()  # the target read the text
        self.foreign = False  # something else read it first: the target's read can't be seen
        self.lost = False  # the clipboard was emptied: a new copy, or our restore


class _ClipboardOwner:
    """A hidden message-only window, with its own message loop, that owns the
    clipboard while a paste is pending. Windows asks it for the text
    (WM_RENDERFORMAT) at the moment an app reads it."""

    def __init__(self) -> None:
        self.hwnd = 0
        self.current: _Offer | None = None
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), name="clipboard owner", daemon=True).start()
        ready.wait(5)
        if not self.hwnd:
            raise TextInsertionError(
                "Could not set up the clipboard for pasting. Restart Whisper Anywhere and try again."
            )

    def _run(self, ready: threading.Event) -> None:
        try:
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = win32api.GetModuleHandle(None)
            window_class.lpszClassName = f"WhisperAnywhereClipboard{id(self)}"
            window_class.lpfnWndProc = {
                WM_RENDERFORMAT: self._render_format,
                WM_RENDERALLFORMATS: self._render_all_formats,
                WM_DESTROYCLIPBOARD: lambda *message: self.emptied() or 0,
            }
            self.hwnd = win32gui.CreateWindowEx(
                0, win32gui.RegisterClass(window_class), "", 0, 0, 0, 0, 0,
                win32con.HWND_MESSAGE, 0, window_class.hInstance, None,
            )
        except Exception:
            return
        finally:
            ready.set()
        win32gui.PumpMessages()

    def _render_format(self, hwnd: int, message: int, fmt: int, lparam: int) -> int:
        if fmt == CF_UNICODETEXT:
            reader = win32clipboard.GetOpenClipboardWindow()
            self.rendered(_window_pid(reader) if reader else 0)
        return 0

    def _render_all_formats(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        # The window is going away with the text still offered: leave the text itself.
        offer = self.current
        try:
            win32clipboard.OpenClipboard(hwnd)
        except Exception:
            return 0
        try:
            if offer is not None and win32clipboard.GetClipboardOwner() == hwnd:
                offer.foreign = True  # whoever reads it now can't be seen
                win32clipboard.SetClipboardData(CF_UNICODETEXT, offer.text)
        except Exception:
            pass
        finally:
            win32clipboard.CloseClipboard()
        return 0

    def rendered(self, reader_pid: int) -> None:
        """An app is reading the text right now. Only the target's own read
        after the Ctrl+V is the paste. After the Ctrl+V any other reader gets
        the text too, but then the target's own read can no longer be seen."""
        offer = self.current
        if offer is None:
            return
        target = reader_pid in offer.pids
        if not offer.pasted and not target:
            # Nothing pastes yet: a monitor that ignores the private formats
            # and reads every new copy (Free Download Manager does, within
            # 4 ms) gets nothing, and the text stays rendered on request.
            return
        try:
            win32clipboard.SetClipboardData(CF_UNICODETEXT, offer.text)
        except Exception:
            return
        if offer.pasted and target:
            offer.read.set()
        else:
            offer.foreign = True

    def emptied(self) -> None:
        """WM_DESTROYCLIPBOARD: someone emptied the clipboard; the offer is gone."""
        offer, self.current = self.current, None
        if offer is not None:
            offer.lost = True


class TextInserter:
    def __init__(self) -> None:
        self._restore_lock = threading.Lock()
        self._pending_restore: _Offer | None = None
        self._saved_clipboard: dict[int, bytes] | None = None
        self._owner: _ClipboardOwner | None = None  # made at the first paste

    def get_foreground_window(self) -> TextTarget | None:
        return self.get_window_target(int(user32.GetForegroundWindow() or 0))

    def get_window_target(self, window_hwnd: int | None) -> TextTarget | None:
        if not window_hwnd or not user32.IsWindow(int(window_hwnd)):
            return None
        hwnd = int(window_hwnd)
        pid = _window_pid(hwnd)
        # Our own Settings, tour and setup windows, and the taskbar, tray and
        # desktop, are no place to type: Enter clicked their focused button.
        if pid == os.getpid() or _window_class(hwnd) in SHELL_WINDOW_CLASSES:
            return None
        return TextTarget(hwnd, _window_title(hwnd), pid, _process_name(pid))

    def insert_text(
        self,
        text: str,
        target: TextTarget | None,
        settings: dict[str, Any],
        cancel: threading.Event | None = None,
    ) -> str:
        """Returns "" when the text and the send key were delivered, or a note
        when the text arrived but the send key was skipped. Raises
        TextInsertionError when the text was not delivered."""
        prepared = self.prepare_text(text, settings)
        if not prepared:
            return ""
        if target is None:
            raise TextInsertionError(
                "No app was in front to type into, so nothing was typed. The text is in Last transcription."
            )
        # The user moved to another window while the clip was transcribed.
        # Typing there could send the words to the wrong chat (with Enter on,
        # at once), and pulling the old window back would cover a game.
        self._check_window(target, SWITCHED)
        theirs, ours = _integrity_level(target.process_id), _integrity_level(os.getpid())
        if theirs and ours and theirs > ours:
            # Windows drops our keys into it without a word.
            raise TextInsertionError(
                f"{target.process_name or 'The app'} runs as administrator, so Windows blocks typing into it. "
                "Run Whisper Anywhere as administrator too, or copy the text from Last transcription."
            )
        _check_cancel(cancel, "Cancelled: nothing was typed.")
        method = settings.get("insert_method", "auto")
        send_key = _send_key_setting(settings)
        if method == "type" or (method != "paste" and foreground_is_fullscreen()):
            # Game chat boxes often ignore Ctrl+V or have no paste at all, but
            # every text box accepts typed characters. Nothing touches the
            # clipboard on this path.
            self._type_text(prepared, target, cancel)
            skipped, done = "", "typed"
        else:
            restore = bool(settings.get("restore_clipboard", True))
            skipped, done = self._paste(prepared, target, restore, cancel), "pasted"
        if send_key is None:
            return ""
        name, keys = send_key
        _check_cancel(cancel, f"Cancelled: the text was {done} but not sent.")
        skipped = skipped or self._send_key_blocker(target)
        if skipped:
            return f"{skipped}, so {name} was not pressed."
        # A pasted newline is just a line break in chat apps; a real key
        # press sends the message.
        if not self._press(keys):
            return f"Windows blocked {name}, so the text was not sent."
        return ""

    def prepare_text(self, text: str, settings: dict[str, Any]) -> str:
        value = " ".join(text.split()).strip()
        if not value:
            return ""

        if bool(settings.get("auto_capitalization", False)):
            # Only a text that starts with a word, maybe after an opening quote
            # or bracket: "10am" is no sentence start, and "iPhone" keeps its case.
            start = 0
            while start < len(value) - 1 and (
                value[start] in "\"'¿¡" or unicodedata.category(value[start]) in ("Ps", "Pi")
            ):
                start += 1
            word = value[start:].split(" ", 1)[0]
            if word[:1].isalpha() and word[1:] == word[1:].lower():
                value = value[:start] + word[0].upper() + value[start + 1 :]

        # Any punctuation already ends it: "…", ",", ":", "。", a closing quote.
        if bool(settings.get("auto_punctuation", False)) and not unicodedata.category(
            value[-1]
        ).startswith("P"):
            value += "."

        if bool(settings.get("add_space_after_text", False)) and not value.endswith(
            " "
        ):
            value += " "

        return value

    def _check_window(self, target: TextTarget, message: str) -> None:
        if int(user32.GetForegroundWindow() or 0) != target.window_hwnd:
            raise TextInsertionError(message)

    def _send_key_blocker(self, target: TextTarget) -> str:
        """Why the send key must not go out now, or ""."""
        held = self._wait_for_modifiers(SEND_KEY_MODIFIER_WAIT_SECONDS)
        if held:
            return f"{held} was held down"
        if int(user32.GetForegroundWindow() or 0) != target.window_hwnd:
            return "You switched windows"
        # Another chat in the same app (a Discord channel, a browser tab)
        # keeps the window but changes its title.
        if target.title and _title_key(_window_title(target.window_hwnd)) != _title_key(target.title):
            return "The chat changed while it transcribed"
        return ""

    def _paste(
        self, text: str, target: TextTarget, restore: bool, cancel: threading.Event | None
    ) -> str:
        """Paste through the clipboard. "" once the target read the text;
        otherwise why the send key must not go out."""
        if self._owner is None:
            if win32clipboard is None:
                raise TextInsertionError(
                    "Clipboard support is unavailable. Reinstall Whisper Anywhere with pywin32 included."
                )
            self._owner = _ClipboardOwner()
        offer = _Offer(text, _window_processes(target.window_hwnd))
        with self._restore_lock:
            pending, self._pending_restore = self._pending_restore, offer
            # A paste whose restore still waits keeps the snapshot from before
            # it, unless something new was copied since.
            if pending is None or pending.lost or not restore:
                self._saved_clipboard = None
            if restore and self._saved_clipboard is None:
                self._saved_clipboard = self._snapshot_clipboard()
        try:
            self._offer(offer)
            held = self._wait_for_modifiers(MODIFIER_WAIT_SECONDS)
            if held:
                raise TextInsertionError(
                    f"{held} was still held down, so nothing was typed. The text is in Last transcription."
                )
            time.sleep(CLIPBOARD_PROPAGATE_SLEEP)
            _check_cancel(cancel, "Cancelled: nothing was typed.")
            self._check_window(target, SWITCHED)
            offer.pasted = True
            if not self._press((VK_CONTROL, VK_V)):
                raise TextInsertionError(
                    "Windows blocked the paste. If the app runs as administrator, run Whisper Anywhere "
                    "as administrator too. The text is in Last transcription."
                )
        except Exception:
            self._restore_if_pending(offer)  # nothing was pasted: put it straight back
            raise
        deadline = time.monotonic() + READ_WAIT_SECONDS
        while not offer.read.wait(READ_POLL_SECONDS):
            if offer.foreign or offer.lost or (cancel is not None and cancel.is_set()):
                break
            if time.monotonic() >= deadline:
                break
        if offer.read.is_set():
            time.sleep(READ_SETTLE_SECONDS)
            self._restore_if_pending(offer)
            return ""
        # Not read yet: restored now, the old clipboard would be pasted instead.
        threading.Thread(
            target=self._restore_after_read, args=(offer,), name="clipboard restore", daemon=True
        ).start()
        _check_cancel(cancel, "Cancelled: the text was pasted but not sent.")
        if offer.foreign:
            return "Something else read the clipboard first"
        if offer.lost:
            raise TextInsertionError(
                "The clipboard changed before the app took the paste. The text is in Last transcription."
            )
        raise TextInsertionError("The app did not take the paste. The text is in Last transcription.")

    def _offer(self, offer: _Offer) -> None:
        """Put the text on the clipboard rendered on request, and the private
        formats as they are."""
        opened = False
        try:
            self._open_clipboard_with_retry(hwnd=self._owner.hwnd)
            opened = True
            win32clipboard.EmptyClipboard()  # tells the old owner, maybe us with an older offer
            self._owner.current = offer
            _offer_on_request(CF_UNICODETEXT)
            _mark_private()
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

    def _restore_after_read(self, offer: _Offer) -> None:
        # Runs on its own thread. A late read still pastes our text, so the old
        # clipboard waits for that read, or for the cap.
        if offer.read.wait(max(0.0, offer.started + READ_CAP_SECONDS - time.monotonic())):
            time.sleep(READ_SETTLE_SECONDS)
        self._restore_if_pending(offer)

    def _restore_if_pending(self, offer: _Offer) -> None:
        with self._restore_lock:
            if self._pending_restore is not offer:
                return  # a newer paste took over, and keeps the snapshot
            self._pending_restore = None
            saved, self._saved_clipboard = self._saved_clipboard, None
        if saved is None:
            return
        owner = self._owner.hwnd
        opened = False
        try:
            self._open_clipboard_with_retry(CLIPBOARD_PATIENT_RETRIES, owner)
            opened = True
            # Leave it alone if the user copied something new meanwhile.
            if win32clipboard.GetClipboardOwner() != owner:
                return
            win32clipboard.EmptyClipboard()
            for fmt, data in saved.items():
                handle = _new_handle(fmt, data)
                if handle:
                    try:
                        win32clipboard.SetClipboardData(fmt, handle)
                    except Exception:
                        _free_handle(fmt, handle)
            _mark_private()  # no new copy: keep it out of Win+V history and monitors
        except Exception:
            pass
        finally:
            if opened:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass

    def _snapshot_clipboard(self) -> dict[int, bytes] | None:
        """Every clipboard format except GDI handles, as raw bytes: text, rich
        text, images (DIB), copied files (CF_HDROP) and EMF pictures."""
        if win32clipboard is None:
            return None
        saved: dict[int, bytes] = {}
        opened = False
        try:
            self._open_clipboard_with_retry(CLIPBOARD_PATIENT_RETRIES)
            opened = True
            fmt = win32clipboard.EnumClipboardFormats(0)
            while fmt:
                if fmt not in HANDLE_FORMATS:
                    try:
                        data = _handle_bytes(fmt, win32clipboard.GetClipboardDataHandle(fmt))
                        if data is not None:
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

    def _wait_for_modifiers(self, seconds: float) -> str:
        """Wait up to `seconds` for Shift, Ctrl, Alt and Win to be let go; the
        name of one still down, or ""."""
        deadline = time.monotonic() + seconds
        waited = False
        while True:
            held = next((name for vk, name in MODIFIER_KEYS if user32.GetAsyncKeyState(vk) & 0x8000), "")
            if not held:
                if waited:
                    time.sleep(MODIFIER_RELEASE_SETTLE)
                return ""
            if time.monotonic() >= deadline:
                return held
            waited = True
            time.sleep(MODIFIER_POLL_SLEEP)

    def _press(self, vks: tuple[int, ...]) -> bool:
        """Hold the keys down in order, then let go in reverse. False if
        Windows refused them."""
        down: list[int] = []
        try:
            for vk in vks:
                if not _send_key_event(vk, 0):
                    return False
                down.append(vk)
                time.sleep(KEY_HOLD_SLEEP)
            return True
        finally:
            for vk in reversed(down):
                _send_key_event(vk, KEYEVENTF_KEYUP)
                time.sleep(KEY_SETTLE_SLEEP)

    def set_clipboard_text(self, text: str, private: bool = False) -> None:
        if win32clipboard is None or win32con is None:
            raise TextInsertionError(
                "Clipboard support is unavailable. Reinstall Whisper Anywhere with pywin32 included."
            )
        opened = False
        try:
            self._open_clipboard_with_retry()
            opened = True
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(CF_UNICODETEXT, text)
            if private:
                _mark_private()
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

    def _open_clipboard_with_retry(self, attempts: int = CLIPBOARD_RETRIES, hwnd: int | None = None) -> None:
        if win32clipboard is None:
            raise TextInsertionError(
                "Clipboard support is unavailable. Reinstall Whisper Anywhere with pywin32 included."
            )
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                win32clipboard.OpenClipboard(hwnd)
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(CLIPBOARD_RETRY_SLEEP)
        if last_error is not None:
            raise last_error

    def _type_text(self, text: str, target: TextTarget, cancel: threading.Event | None) -> None:
        held = self._wait_for_modifiers(MODIFIER_WAIT_SECONDS)
        if held:
            raise TextInsertionError(
                f"{held} was still held down, so nothing was typed. The text is in Last transcription."
            )
        data = text.encode("utf-16-le")
        # One key press per UTF-16 unit; an emoji goes out as its two halves.
        for index in range(0, len(data), 2):
            _check_cancel(cancel, "Cancelled while typing, so the rest was not typed or sent.")
            self._check_window(
                target,
                "You switched windows while it typed, so typing stopped and nothing was sent. "
                "The text is in Last transcription."
                if index
                else SWITCHED,
            )
            self._send_unicode_unit(int.from_bytes(data[index : index + 2], "little"))
            time.sleep(TYPE_CHAR_SLEEP)

    def _send_unicode_unit(self, unit: int) -> None:
        # Unicode key events arrive as typed characters (VK_PACKET), so games
        # do not read them as movement or hotkeys.
        events = (INPUT * 2)(
            *(
                INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, unit, flags, 0, REPLAY_MARKER)))
                for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            )
        )
        if user32.SendInput(2, events, ctypes.sizeof(INPUT)) != 2:
            raise TextInsertionError(
                "Windows blocked typing into the focused app. If it runs as administrator, run Whisper Anywhere as administrator too."
            )
