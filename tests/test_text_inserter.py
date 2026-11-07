from __future__ import annotations

import ctypes
import os
import threading
from types import SimpleNamespace

import pytest
import pywintypes

import core.text_inserter as text_module
from core.hotkey_listener import REPLAY_MARKER
from core.text_inserter import InsertionCancelled, TextInsertionError, TextTarget

DELAYED = object()  # CF_UNICODETEXT put on the clipboard to be rendered on request
TARGET, OTHER, OWNER = 111, 222, 0xC1
TARGET_PID, MONITOR_PID = 4242, 999
CF_BITMAP, CF_DIB, CF_ENHMETAFILE, CF_HDROP = 2, 8, 14, 15
VK_SHIFT, VK_CONTROL, VK_ALT, VK_V, VK_RETURN = 0x10, 0x11, 0x12, 0x56, 0x0D
SCAN_CODES = {VK_CONTROL: 0x1D, VK_V: 0x2F, VK_RETURN: 0x1C}
DISCORD = TextTarget(TARGET, "#team | Gaming - Discord", TARGET_PID, "discord.exe")
BEN = {  # how Ben dictates into Discord and games
    "insert_method": "auto",
    "restore_clipboard": True,
    "send_key": "enter",
    "auto_punctuation": True,
}
REAL_HANDLE_BYTES = text_module._handle_bytes
REAL_NEW_HANDLE = text_module._new_handle
REAL_FREE_HANDLE = text_module._free_handle


class FakeClipboard:
    """win32clipboard over a dict. Emptying it tells the old owner window, as
    Windows' WM_DESTROYCLIPBOARD does."""

    def __init__(self, log: list) -> None:
        self.data: dict[int, object] = {13: "keep me"}
        self.log = log
        self.formats: dict[str, int] = {}
        self.owner = 0
        self.opener = 0
        self.busy = 0  # opens that fail, as while another app holds the clipboard
        self.on_empty = None  # the owner window hearing that its clipboard was emptied
        self.on_close = None  # a clipboard listener reacting to a change

    def OpenClipboard(self, hwnd: int | None = None) -> None:
        if self.busy:
            self.busy -= 1
            raise OSError("Access is denied.")
        self.opener = hwnd or 0

    def CloseClipboard(self) -> None:
        if self.on_close is not None:
            self.on_close()

    def EmptyClipboard(self) -> None:
        if self.owner == OWNER and self.on_empty is not None:
            self.on_empty()
        self.log.append("empty")
        self.data.clear()
        self.owner = self.opener

    def SetClipboardData(self, fmt: int, value: object) -> None:
        self.data[fmt] = value

    def EnumClipboardFormats(self, fmt: int) -> int:
        later = sorted(f for f in self.data if f > fmt)
        return later[0] if later else 0

    def GetClipboardDataHandle(self, fmt: int) -> object:
        return self.data[fmt]  # the "handle" is the data itself; see _handle_bytes below

    def GetClipboardOwner(self) -> int:
        return self.owner

    def GetOpenClipboardWindow(self) -> int:
        if not self.opener:  # as pywin32: an error, not 0, for a clipboard opened with no window
            raise pywintypes.error(0, "GetOpenClipboardWindow", "No error message is available")
        return self.opener

    def RegisterClipboardFormat(self, name: str) -> int:
        return self.formats.setdefault(name, 49000 + len(self.formats))


class FakeUser32:
    """The user32 calls text insertion makes. SendInput only records."""

    def __init__(self, log: list) -> None:
        self.log = log
        self.foreground: int | None = TARGET
        self.titles = {TARGET: DISCORD.title}
        self.held: set[int] = set()
        self.refuse = False  # Windows refuses our keys, as for an app run as administrator
        self.on_key = lambda vk, up: None  # the app or the user, acting on one of our keys
        self.typed: list[tuple[str, int | None]] = []  # typed characters and the window in front

    def GetForegroundWindow(self) -> int | None:
        return self.foreground

    def IsWindow(self, hwnd: int) -> bool:
        return True

    def GetAsyncKeyState(self, vk: int) -> int:
        return -0x8000 if vk in self.held else 0

    def MapVirtualKeyW(self, vk: int, map_type: int) -> int:
        return SCAN_CODES.get(vk, 0)

    def SendInput(self, count: int, events, size: int) -> int:
        assert size == ctypes.sizeof(text_module.INPUT)
        if self.refuse:
            return 0
        for index in range(count):
            key = events[index].u.ki
            up = bool(key.dwFlags & 0x0002)
            if key.dwFlags & 0x0004:  # KEYEVENTF_UNICODE: a typed character
                if not up:
                    self.typed.append((chr(key.wScan), self.foreground))
                continue
            self.log.append(("up" if up else "down", key.wVk, key.wScan))
            self.on_key(key.wVk, up)
        return count


class FakeOwner(text_module._ClipboardOwner):
    """The clipboard owner without its window: the fake clipboard tells it
    when it is emptied, and FakeApp asks it for the text."""

    def __init__(self, clipboard: FakeClipboard) -> None:
        self.hwnd = OWNER
        self.current = None
        clipboard.on_empty = self.emptied


class FakeApp:
    """The app in front. Its page reads the clipboard when it gets to the
    Ctrl+V; text offered on request is rendered for it right then."""

    def __init__(self, clipboard: FakeClipboard, owner: FakeOwner, log: list) -> None:
        self.clipboard = clipboard
        self.owner = owner
        self.log = log
        self.pasted: list[object] = []
        self.seen: dict[int, object] = {}

    def paste(self, pid: int = TARGET_PID) -> None:
        """Read the clipboard as the app with that pid, through the clipboard
        window that is its pid here; 0 opens it with no window, as Chromium
        apps (Discord, Chrome, Edge) do from a worker thread."""
        self.seen = dict(self.clipboard.data)
        if self.clipboard.data.get(13) is DELAYED:
            self.clipboard.opener = pid
            try:  # Windows asks the owner window: WM_RENDERFORMAT
                self.owner._render_format(OWNER, text_module.WM_RENDERFORMAT, 13, 0)
            except Exception:
                pass  # pywin32 prints a failed WndProc's error; the reader just gets no text
        self.log.append(("read", pid))
        text = self.clipboard.data.get(13)
        self.pasted.append(None if text is DELAYED else text)  # None: the owner handed over nothing


def forbidden(what: str):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a test reached {what}")

    return refuse


@pytest.fixture(autouse=True)
def windows(monkeypatch):
    """Nothing in this file reaches the real keyboard, clipboard or windows."""
    log: list = []
    fake = SimpleNamespace(log=log, user32=FakeUser32(log), clipboard=FakeClipboard(log))
    for name in ("SendInput", "keybd_event", "OpenClipboard", "EmptyClipboard", "SetClipboardData"):
        monkeypatch.setattr(ctypes.windll.user32, name, forbidden(f"user32.{name}"))
    monkeypatch.setattr(text_module, "user32", fake.user32)
    monkeypatch.setattr(text_module, "win32clipboard", fake.clipboard)
    monkeypatch.setattr(text_module, "_ClipboardOwner", forbidden("the real clipboard owner window"))
    monkeypatch.setattr(
        text_module, "_offer_on_request", lambda fmt: fake.clipboard.SetClipboardData(fmt, DELAYED)
    )
    monkeypatch.setattr(text_module, "_handle_bytes", lambda fmt, handle: handle)
    monkeypatch.setattr(text_module, "_new_handle", lambda fmt, data: data)
    monkeypatch.setattr(text_module, "_free_handle", lambda fmt, handle: None)
    monkeypatch.setattr(text_module, "_window_processes", lambda hwnd: frozenset({TARGET_PID}))
    monkeypatch.setattr(text_module, "_window_title", lambda hwnd: fake.user32.titles.get(hwnd, ""))
    monkeypatch.setattr(text_module, "_integrity_level", lambda pid: 0x2000)
    monkeypatch.setattr(text_module, "foreground_is_fullscreen", lambda: False)
    monkeypatch.setattr(text_module, "READ_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(text_module, "READ_CAP_SECONDS", 0.6)
    monkeypatch.setattr(text_module.time, "sleep", lambda seconds: None)
    yield fake
    join_restores()


@pytest.fixture
def paste(windows, monkeypatch):
    """Pasting into the fake app, which reads the clipboard when it gets Ctrl+V."""
    monkeypatch.setattr(text_module, "_window_pid", lambda hwnd: hwnd)  # see FakeApp.paste
    inserter = text_module.TextInserter()
    inserter._owner = owner = FakeOwner(windows.clipboard)
    app = FakeApp(windows.clipboard, owner, windows.log)
    windows.user32.on_key = lambda vk, up: app.paste() if vk == VK_V and not up else None
    return SimpleNamespace(inserter=inserter, app=app, owner=owner, **vars(windows))


def join_restores() -> None:
    for thread in threading.enumerate():  # a restore that waits for a late read
        if thread.name == "clipboard restore":
            thread.join(5)


def keys(log: list) -> list[tuple[str, int]]:
    return [(event[0], event[1]) for event in log if event[0] in ("down", "up")]


def enter_pressed(log: list) -> bool:
    return ("down", VK_RETURN) in keys(log)


def steps(log: list) -> list[str]:
    """The clipboard emptied (by the offer or the restore), the app's read and Enter, in order."""
    named = []
    for event in log:
        if event == "empty":
            named.append("empty")
        elif event[0] == "read":
            named.append("read")
        elif event[:2] == ("down", VK_RETURN):
            named.append("enter")
    return named


def user_copies(clipboard: FakeClipboard, text: str) -> None:
    clipboard.OpenClipboard(0x999)
    clipboard.EmptyClipboard()
    clipboard.SetClipboardData(13, text)
    clipboard.CloseClipboard()


# ------------------------------------------------------------------ prepare_text


def test_prepare_text_formats_whitespace_and_empty_input() -> None:
    inserter = text_module.TextInserter()

    assert inserter.prepare_text("   ", {}) == ""
    assert (
        inserter.prepare_text(
            "  hello   there ",
            {
                "auto_capitalization": True,
                "auto_punctuation": True,
                "add_space_after_text": True,
                "press_enter_after_text": False,
            },
        )
        == "Hello there. "
    )


@pytest.mark.parametrize(
    "spoken",
    ["Wait for it…", "Τι κάνεις;", "你好吗？", "好的。", 'He said "stop!"', "(it works!)", "The list is:", "so I think,"],
)
def test_no_full_stop_after_punctuation(spoken: str) -> None:
    # TX-8: "Wait for it…" became "Wait for it…." and "好的。" became "好的。.".
    assert text_module.TextInserter().prepare_text(spoken, {"auto_punctuation": True}) == spoken


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("10am meeting moved", "10am meeting moved"),  # was "10Am meeting moved"
        ("iPhone sync is broken", "iPhone sync is broken"),  # was "IPhone"
        ("hello there", "Hello there"),
        ('"hello" she said', '"Hello" she said'),
        ("(maybe) later", "(Maybe) later"),
        ('" hello', '" hello'),  # no word right after the quote
    ],
)
def test_capitalization_only_starts_a_text_that_starts_with_a_word(spoken: str, expected: str) -> None:
    assert text_module.TextInserter().prepare_text(spoken, {"auto_capitalization": True}) == expected


# ------------------------------------------------------------------ the target window


def test_a_window_target_records_its_title_and_process(monkeypatch) -> None:
    # Per-app profiles need the exe, and the chat check needs the title at release.
    monkeypatch.setattr(text_module, "_window_pid", lambda hwnd: TARGET_PID)
    monkeypatch.setattr(text_module, "_window_class", lambda hwnd: "Chrome_WidgetWin_1")
    monkeypatch.setattr(text_module, "_process_name", lambda pid: "discord.exe")

    assert text_module.TextInserter().get_window_target(TARGET) == DISCORD


@pytest.mark.parametrize(
    "pid,window_class",
    [
        (os.getpid(), "Qt6110QWindowIcon"),
        (TARGET_PID, "Shell_TrayWnd"),
        (TARGET_PID, "Shell_SecondaryTrayWnd"),
        (TARGET_PID, "NotifyIconOverflowWindow"),
        (TARGET_PID, "Progman"),
        (TARGET_PID, "WorkerW"),
    ],
)
def test_our_own_windows_and_the_shell_are_no_target(monkeypatch, pid: int, window_class: str) -> None:
    # UA-7: our Settings, tour and setup windows became paste targets, and
    # Enter clicked their focused button.
    monkeypatch.setattr(text_module, "_window_pid", lambda hwnd: pid)
    monkeypatch.setattr(text_module, "_window_class", lambda hwnd: window_class)
    monkeypatch.setattr(text_module, "_process_name", lambda pid: "whatever.exe")

    assert text_module.TextInserter().get_window_target(TARGET) is None


def test_no_window_in_front_means_no_target(windows) -> None:
    # GetForegroundWindow returns NULL while a window loses activation.
    windows.user32.foreground = None

    assert text_module.TextInserter().get_foreground_window() is None


def test_with_no_app_in_front_nothing_is_typed(paste) -> None:
    with pytest.raises(TextInsertionError, match="No app was in front"):
        paste.inserter.insert_text("hello", None, BEN)

    assert paste.log == []
    assert paste.clipboard.data == {13: "keep me"}


# ------------------------------------------------------------------ the paste, read on request


def test_enter_waits_for_the_app_to_read_the_paste_and_the_old_clipboard_comes_back_first(paste) -> None:
    # TX-1: a Chromium page reads the clipboard only when it gets to the
    # Ctrl+V; when it was busy it read the old clipboard, and Enter sent that.
    note = paste.inserter.insert_text("ship it", DISCORD, BEN)

    assert steps(paste.log) == ["empty", "read", "empty", "enter"]  # offer, read, restore, Enter
    assert paste.app.pasted == ["ship it."]
    assert paste.clipboard.data[13] == "keep me"
    assert note == ""


def test_a_page_that_gets_to_the_paste_late_still_pastes_the_new_text(paste, monkeypatch) -> None:
    # The window answers at once while its page reaches the Ctrl+V later.
    monkeypatch.setattr(text_module, "READ_WAIT_SECONDS", 5)
    paste.user32.on_key = lambda vk, up: (
        threading.Timer(0.3, paste.app.paste).start() if vk == VK_V and not up else None
    )

    note = paste.inserter.insert_text("meeting at three", DISCORD, BEN)

    assert steps(paste.log) == ["empty", "read", "empty", "enter"]
    assert paste.app.pasted == ["meeting at three."]
    assert note == ""


def test_an_app_that_never_reads_the_paste_gets_no_enter_and_the_old_clipboard_at_the_cap(
    paste, monkeypatch
) -> None:
    # TX-4: an app that did not answer for 2 s lost the old clipboard for good.
    monkeypatch.setattr(text_module, "READ_CAP_SECONDS", 2)
    paste.user32.on_key = lambda vk, up: None

    with pytest.raises(TextInsertionError, match="did not take the paste.*Last transcription"):
        paste.inserter.insert_text("ship it", DISCORD, BEN)

    assert not enter_pressed(paste.log)
    assert paste.clipboard.data[13] is DELAYED  # never the old clipboard under a paste still on its way
    join_restores()
    assert paste.clipboard.data[13] == "keep me"


def test_a_read_after_the_wait_still_pastes_the_text_and_the_old_clipboard_follows(
    paste, monkeypatch
) -> None:
    monkeypatch.setattr(text_module, "READ_CAP_SECONDS", 30)
    paste.user32.on_key = lambda vk, up: None

    with pytest.raises(TextInsertionError, match="did not take the paste"):
        paste.inserter.insert_text("ship it", DISCORD, BEN)
    paste.app.paste()  # the app gets there in the end
    join_restores()

    assert paste.app.pasted == ["ship it."]
    assert paste.clipboard.data[13] == "keep me"
    assert not enter_pressed(paste.log)


def test_an_app_reading_with_no_window_gets_the_paste_and_enter(paste) -> None:
    # Chromium apps (Discord, Chrome, Edge) read the clipboard on a worker
    # thread that opens it with no window. pywin32 raised for that inside
    # WM_RENDERFORMAT, so v0.2.6 handed them nothing: "The app did not take
    # the paste", in every Chromium app.
    paste.user32.on_key = lambda vk, up: paste.app.paste(0) if vk == VK_V and not up else None

    note = paste.inserter.insert_text("ship it", DISCORD, BEN)

    assert paste.app.pasted == ["ship it."]
    assert steps(paste.log) == ["empty", "read", "empty", "enter"]  # offer, read, restore, Enter
    assert note == ""


def test_another_reader_first_means_no_enter_and_no_restore_under_the_paste(paste, monkeypatch) -> None:
    # A monitor that ignores the private formats takes the rendering, and
    # then the app's own read can no longer be seen.
    monkeypatch.setattr(text_module, "READ_CAP_SECONDS", 2)

    def monitor_then_app(vk: int, up: bool) -> None:
        if vk == VK_V and not up:
            paste.app.paste(MONITOR_PID)
            paste.app.paste()

    paste.user32.on_key = monitor_then_app

    note = paste.inserter.insert_text("ship it", DISCORD, BEN)

    assert not enter_pressed(paste.log)
    assert note == "Something else read the clipboard first, so Enter was not pressed."
    assert paste.app.pasted == ["ship it.", "ship it."]
    assert paste.clipboard.data[13] == "ship it."  # the app may still be on its way to the paste
    join_restores()
    assert paste.clipboard.data[13] == "keep me"


@pytest.mark.parametrize("monitor_pid", [MONITOR_PID, 0])  # one with a clipboard window; one with none
def test_a_monitor_reading_before_the_ctrl_v_gets_nothing_and_enter_still_goes_out(paste, monitor_pid) -> None:
    # Monitors that ignore the private formats read every new copy at once,
    # long before our Ctrl+V. Rendered for them, the text could no longer be
    # rendered on request, and the app's own read was never seen.
    def monitor() -> None:
        if paste.clipboard.data.get(13) is DELAYED:  # our text was just offered
            paste.clipboard.on_close = None
            paste.app.paste(monitor_pid)

    paste.clipboard.on_close = monitor

    note = paste.inserter.insert_text("ship it", DISCORD, BEN)

    assert enter_pressed(paste.log)
    assert paste.app.pasted == [None, "ship it."]  # the monitor got nothing, the app its text
    assert note == ""


def test_the_text_is_left_on_the_clipboard_when_the_owner_window_goes_away(paste) -> None:
    offer = text_module._Offer("ship it.", frozenset({TARGET_PID}))
    paste.inserter._owner = paste.owner
    paste.clipboard.OpenClipboard(OWNER)
    paste.clipboard.EmptyClipboard()
    paste.owner.current = offer
    paste.clipboard.SetClipboardData(13, DELAYED)

    paste.owner._render_all_formats(OWNER, text_module.WM_RENDERALLFORMATS, 0, 0)  # WM_RENDERALLFORMATS

    assert paste.clipboard.data[13] == "ship it."
    assert offer.foreign  # whoever reads it now can't be seen


def test_a_read_before_the_ctrl_v_is_no_paste(paste) -> None:
    # The app's own clipboard listener (a VM's clipboard sync) read the new
    # text before the paste: that read says nothing about the paste.
    def listener() -> None:
        if paste.clipboard.data.get(13) is DELAYED:  # our text was just offered
            paste.clipboard.on_close = None
            paste.app.paste()

    paste.clipboard.on_close = listener

    note = paste.inserter.insert_text("ship it", DISCORD, BEN)

    assert not enter_pressed(paste.log)
    assert note == "Something else read the clipboard first, so Enter was not pressed."


def test_a_copy_made_while_waiting_for_the_read_is_kept(paste) -> None:
    def user_copies_after_the_paste(vk: int, up: bool) -> None:
        if vk == VK_CONTROL and up:
            user_copies(paste.clipboard, "new copy")

    paste.user32.on_key = user_copies_after_the_paste

    with pytest.raises(TextInsertionError, match="clipboard changed"):
        paste.inserter.insert_text("ship it", DISCORD, BEN)
    join_restores()

    assert paste.clipboard.data[13] == "new copy"


def test_a_new_paste_keeps_the_snapshot_from_before_the_first(paste, monkeypatch) -> None:
    monkeypatch.setattr(text_module, "READ_CAP_SECONDS", 30)
    paste.user32.on_key = lambda vk, up: None
    with pytest.raises(TextInsertionError):
        paste.inserter.insert_text("first", DISCORD, BEN)
    first = paste.inserter._pending_restore
    paste.user32.on_key = lambda vk, up: paste.app.paste() if vk == VK_V and not up else None

    assert paste.inserter.insert_text("second", DISCORD, BEN) == ""

    assert paste.clipboard.data[13] == "keep me"  # not "first."
    first.read.set()  # let the first paste's restore, which the second took over, finish


def test_the_old_clipboard_comes_back_while_another_app_briefly_holds_it(paste) -> None:
    # A clipboard manager or remote desktop holding the clipboard for a moment
    # at restore time lost the user's own clipboard for good.
    def read_then_hold(vk: int, up: bool) -> None:
        if vk == VK_V and not up:
            paste.app.paste()
            paste.clipboard.busy = 5

    paste.user32.on_key = read_then_hold

    note = paste.inserter.insert_text("hello", DISCORD, BEN)

    assert paste.clipboard.data[13] == "keep me"
    assert note == ""


def test_a_refused_paste_is_an_error_and_the_old_clipboard_comes_straight_back(paste) -> None:
    # TX-5: pynput never looked at SendInput's answer, so a paste an elevated
    # app refused said "Done" and restored the clipboard as if it had worked.
    paste.user32.refuse = True

    with pytest.raises(TextInsertionError, match="administrator"):
        paste.inserter.insert_text("restart the service", DISCORD, BEN)

    assert paste.app.pasted == []
    assert paste.clipboard.data[13] == "keep me"


def test_copied_files_and_pictures_survive_the_restore(paste) -> None:
    # TX-7: copied files (pywin32 hands CF_HDROP back as a tuple of names)
    # and EMF pictures did not come back after a dictation.
    copied = {CF_HDROP: b"files", CF_ENHMETAFILE: b"emf bits", CF_DIB: b"dib", 49330: b"shell ids"}
    paste.clipboard.data = {**copied, CF_BITMAP: 0xB17, 13: "keep me"}

    paste.inserter.insert_text("here are the files", DISCORD, BEN)

    assert {fmt: paste.clipboard.data.get(fmt) for fmt in copied} == copied
    assert CF_BITMAP not in paste.clipboard.data  # a GDI handle; Windows rebuilds it from CF_DIB


def test_the_paste_and_the_restore_stay_out_of_clipboard_history(paste) -> None:
    # B22: the restored clipboard went back into Win+V history and clipboard
    # monitors as a fresh copy after every dictation.
    paste.inserter.insert_text("ship it", DISCORD, BEN)

    assert paste.clipboard.data[13] == "keep me"
    for name, value in text_module.PRIVATE_CLIPBOARD_FORMATS.items():
        fmt = paste.clipboard.formats[name]
        assert paste.app.seen[fmt] == value  # on the clipboard the app pasted from
        assert paste.clipboard.data[fmt] == value  # and on the one put back


def test_clipboard_memory_round_trips_as_raw_bytes() -> None:
    data = b"\x14\x00\x00\x00 copied files \x00\x00"
    handle = REAL_NEW_HANDLE(CF_HDROP, data)
    try:
        assert REAL_HANDLE_BYTES(CF_HDROP, handle) == data
    finally:
        REAL_FREE_HANDLE(CF_HDROP, handle)


def test_emf_pictures_round_trip_through_their_bits() -> None:
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.CreateEnhMetaFileW.restype = gdi32.CloseEnhMetaFile.restype = ctypes.c_void_p
    gdi32.CreateEnhMetaFileW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_wchar_p)
    gdi32.CloseEnhMetaFile.argtypes = (ctypes.c_void_p,)
    gdi32.Rectangle.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    drawing = gdi32.CreateEnhMetaFileW(None, None, None, None)
    gdi32.Rectangle(drawing, 0, 0, 40, 30)
    picture = gdi32.CloseEnhMetaFile(drawing)
    copy = 0
    try:
        bits = REAL_HANDLE_BYTES(CF_ENHMETAFILE, picture)
        copy = REAL_NEW_HANDLE(CF_ENHMETAFILE, bits)
        assert bits and REAL_HANDLE_BYTES(CF_ENHMETAFILE, copy) == bits
    finally:
        REAL_FREE_HANDLE(CF_ENHMETAFILE, picture)
        if copy:
            REAL_FREE_HANDLE(CF_ENHMETAFILE, copy)


# ------------------------------------------------------------------ our keys


def test_the_paste_and_enter_are_real_keys_with_scan_codes(paste) -> None:
    # TX-6: pynput sent "v" as a typed character on layouts without a Latin
    # V, so the paste was no Ctrl+V.
    paste.inserter.insert_text("ship it", DISCORD, BEN)

    assert [event for event in paste.log if event[0] in ("down", "up")] == [
        ("down", VK_CONTROL, 0x1D),
        ("down", VK_V, 0x2F),
        ("up", VK_V, 0x2F),
        ("up", VK_CONTROL, 0x1D),
        ("down", VK_RETURN, 0x1C),
        ("up", VK_RETURN, 0x1C),
    ]
    assert paste.user32.typed == []


@pytest.mark.parametrize(
    "settings,sent",
    [
        ({"send_key": "ctrl+enter"}, [("down", VK_CONTROL), ("down", VK_RETURN), ("up", VK_RETURN), ("up", VK_CONTROL)]),
        ({"send_key": "none", "press_enter_after_text": True}, []),
        ({"press_enter_after_text": True}, [("down", VK_RETURN), ("up", VK_RETURN)]),
        ({}, []),
    ],
)
def test_the_send_key_setting(paste, settings: dict, sent: list) -> None:
    paste.inserter.insert_text("ship it", DISCORD, settings)

    assert keys(paste.log)[4:] == sent  # after the paste's Ctrl+V


def test_a_refused_enter_is_a_note(paste) -> None:
    def read_then_refuse(vk: int, up: bool) -> None:
        if vk == VK_V and not up:
            paste.app.paste()
            paste.user32.refuse = True

    paste.user32.on_key = read_then_refuse

    assert paste.inserter.insert_text("ship it", DISCORD, BEN) == "Windows blocked Enter, so the text was not sent."


def test_an_app_run_as_administrator_is_refused_before_the_clipboard_is_touched(paste, monkeypatch) -> None:
    monkeypatch.setattr(text_module, "_integrity_level", lambda pid: 0x3000 if pid == TARGET_PID else 0x2000)
    task_manager = TextTarget(TARGET, "Task Manager", TARGET_PID, "taskmgr.exe")

    with pytest.raises(TextInsertionError, match="taskmgr.exe runs as administrator"):
        paste.inserter.insert_text("restart the service", task_manager, BEN)

    assert paste.log == []


def test_an_app_whose_level_windows_will_not_tell_is_still_tried(paste, monkeypatch) -> None:
    # Anti-cheat can hide a game's token; that is no reason to refuse it.
    monkeypatch.setattr(text_module, "_integrity_level", lambda pid: None if pid == TARGET_PID else 0x2000)

    assert paste.inserter.insert_text("gg", DISCORD, BEN) == ""
    assert enter_pressed(paste.log)


def test_every_key_we_send_carries_the_marker_the_shortcut_hook_skips(paste) -> None:
    # The paste's Ctrl+V, the Enter after it and typed text are injected keys
    # that could fire a V or Enter shortcut. The hooks run in their own
    # process and can't see when we paste, so each key says it is ours.
    markers: list[int] = []
    send = paste.user32.SendInput

    def marked(count: int, events, size: int) -> int:
        markers.extend(events[index].u.ki.dwExtraInfo for index in range(count))
        return send(count, events, size)

    paste.user32.SendInput = marked
    paste.inserter.insert_text("hello", DISCORD, {"restore_clipboard": False, "send_key": "enter"})
    paste.inserter.insert_text("gg", DISCORD, {"insert_method": "type", "send_key": "none"})

    assert enter_pressed(paste.log) and paste.user32.typed
    assert markers and set(markers) == {REPLAY_MARKER}


def test_input_struct_matches_windows_size() -> None:
    # SendInput rejects every event unless cbSize is the real sizeof(INPUT).
    assert ctypes.sizeof(text_module.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_copied_text_is_marked_private_for_clipboard_history(windows) -> None:
    text_module.TextInserter().set_clipboard_text("secret words", private=True)

    formats = windows.clipboard.formats
    assert set(formats) == set(text_module.PRIVATE_CLIPBOARD_FORMATS)
    assert windows.clipboard.data[formats["CanIncludeInClipboardHistory"]] == b"\0\0\0\0"
    assert windows.clipboard.data[13] == "secret words"


# ------------------------------------------------------------------ typing


def test_full_screen_games_get_typed_text_and_keep_the_clipboard(windows, monkeypatch) -> None:
    monkeypatch.setattr(text_module, "foreground_is_fullscreen", lambda: True)

    text_module.TextInserter().insert_text("gg 👍", DISCORD, {})

    # The emoji is a surrogate pair: two UTF-16 units, each its own key press.
    assert [char for char, _ in windows.user32.typed] == ["g", "g", " ", "\ud83d", "\udc4d"]
    assert windows.log == []


def test_insert_method_type_and_paste_override_auto(paste, monkeypatch) -> None:
    paste.inserter.insert_text("typed", DISCORD, {"insert_method": "type"})
    monkeypatch.setattr(text_module, "foreground_is_fullscreen", lambda: True)
    paste.inserter.insert_text("pasted", DISCORD, {"insert_method": "paste"})

    assert [char for char, _ in paste.user32.typed] == list("typed")
    assert paste.app.pasted == ["pasted"]


def test_typing_checks_that_windows_took_each_key(windows) -> None:
    windows.user32.refuse = True

    with pytest.raises(TextInsertionError, match="administrator"):
        text_module.TextInserter().insert_text("gg", DISCORD, {"insert_method": "type"})


# ------------------------------------------------------------------ the window or chat changed


def test_text_is_not_typed_into_a_window_the_user_switched_to(paste) -> None:
    # Switching to another chat or a game while the clip is transcribed: the
    # words went there (and Enter sent them), or the old window covered the game.
    paste.user32.foreground = OTHER

    with pytest.raises(TextInsertionError, match="switched"):
        paste.inserter.insert_text("secret", DISCORD, BEN)

    assert paste.log == []


def test_alt_tab_finishing_while_the_paste_waits_for_alt_stops_it(paste) -> None:
    # TX-3: Alt was down at the window check; letting go of it finished
    # Alt+Tab, and the Ctrl+V and Enter went to the new window.
    polls = []

    def alt_until_the_switch(vk: int) -> int:
        if vk != VK_ALT:
            return 0
        polls.append(vk)
        if len(polls) < 5:
            return -0x8000
        paste.user32.foreground = OTHER
        return 0

    paste.user32.GetAsyncKeyState = alt_until_the_switch

    with pytest.raises(TextInsertionError, match="switched"):
        paste.inserter.insert_text("ok ship it", DISCORD, BEN)

    assert keys(paste.log) == []
    assert paste.clipboard.data[13] == "keep me"


def test_switching_windows_after_the_paste_skips_enter(paste) -> None:
    def read_then_switch(vk: int, up: bool) -> None:
        if vk == VK_V and not up:
            paste.app.paste()
            paste.user32.foreground = OTHER

    paste.user32.on_key = read_then_switch

    note = paste.inserter.insert_text("ok ship it", DISCORD, BEN)

    assert not enter_pressed(paste.log)
    assert note == "You switched windows, so Enter was not pressed."


def test_typing_stops_when_the_window_changes(windows, monkeypatch) -> None:
    # TX-3: an Alt+Tab out of the game mid-typing sent the rest, and Enter,
    # to the new window.
    monkeypatch.setattr(text_module, "foreground_is_fullscreen", lambda: True)
    send = windows.user32.SendInput

    def switch_after_five(count, events, size):
        result = send(count, events, size)
        if len(windows.user32.typed) == 5:
            windows.user32.foreground = OTHER
        return result

    windows.user32.SendInput = switch_after_five

    with pytest.raises(TextInsertionError, match="switched"):
        text_module.TextInserter().insert_text("push mid now", DISCORD, BEN)

    assert windows.user32.typed == [(char, TARGET) for char in "push "]
    assert keys(windows.log) == []


def test_a_chat_switched_inside_the_app_gets_the_text_but_not_enter(paste) -> None:
    # TX-2: moving from #team to a DM while it transcribed pasted, and sent,
    # the words in the DM.
    paste.user32.titles[TARGET] = "@alice - Discord"

    note = paste.inserter.insert_text("they never read the brief", DISCORD, BEN)

    assert not enter_pressed(paste.log)
    assert paste.app.pasted == ["they never read the brief."]
    assert note == "The chat changed while it transcribed, so Enter was not pressed."


@pytest.mark.parametrize("title_now", ["(3) #team | Gaming - Discord", "• #team | Gaming - Discord"])
def test_unread_counters_are_no_chat_change(paste, title_now: str) -> None:
    paste.user32.titles[TARGET] = title_now

    assert paste.inserter.insert_text("gg", DISCORD, BEN) == ""
    assert enter_pressed(paste.log)


# ------------------------------------------------------------------ held modifiers


@pytest.mark.parametrize("method", ["paste", "type"])
def test_a_modifier_still_held_after_the_wait_types_nothing(paste, monkeypatch, method: str) -> None:
    # SW-2: after 1.5 s the paste and Enter went out anyway, as Shift+Enter
    # (a new line in Discord) or Alt+Enter (full screen in games).
    monkeypatch.setattr(text_module, "MODIFIER_WAIT_SECONDS", 0)
    paste.user32.held = {VK_SHIFT}

    with pytest.raises(TextInsertionError, match="Shift was still held down.*Last transcription"):
        paste.inserter.insert_text("push mid", DISCORD, {**BEN, "insert_method": method})

    assert keys(paste.log) == []
    assert paste.user32.typed == []
    assert paste.clipboard.data[13] == "keep me"


def test_a_modifier_pressed_before_enter_skips_enter(paste, monkeypatch) -> None:
    monkeypatch.setattr(text_module, "SEND_KEY_MODIFIER_WAIT_SECONDS", 0)

    def read_while_alt_goes_down(vk: int, up: bool) -> None:
        if vk == VK_V and not up:
            paste.app.paste()
            paste.user32.held = {VK_ALT}

    paste.user32.on_key = read_while_alt_goes_down

    assert paste.inserter.insert_text("push mid", DISCORD, BEN) == "Alt was held down, so Enter was not pressed."
    assert not enter_pressed(paste.log)


# ------------------------------------------------------------------ cancel


def test_cancel_before_the_paste_types_nothing(paste) -> None:
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(InsertionCancelled):
        paste.inserter.insert_text("ship it", DISCORD, BEN, cancel=cancel)

    assert paste.log == []


def test_cancel_after_the_paste_skips_enter(paste) -> None:
    cancel = threading.Event()

    def read_then_cancel(vk: int, up: bool) -> None:
        if vk == VK_V and not up:
            paste.app.paste()
            cancel.set()

    paste.user32.on_key = read_then_cancel

    with pytest.raises(InsertionCancelled, match="pasted but not sent"):
        paste.inserter.insert_text("ship it", DISCORD, BEN, cancel=cancel)

    assert paste.app.pasted == ["ship it."]
    assert not enter_pressed(paste.log)
    assert paste.clipboard.data[13] == "keep me"


def test_cancel_while_typing_stops_typing(windows) -> None:
    cancel = threading.Event()
    send = windows.user32.SendInput

    def cancel_after_three(count, events, size):
        result = send(count, events, size)
        if len(windows.user32.typed) == 3:
            cancel.set()
        return result

    windows.user32.SendInput = cancel_after_three

    with pytest.raises(InsertionCancelled):
        text_module.TextInserter().insert_text(
            "push mid now", DISCORD, {"insert_method": "type", "send_key": "enter"}, cancel=cancel
        )

    assert [char for char, _ in windows.user32.typed] == list("pus")
    assert keys(windows.log) == []
