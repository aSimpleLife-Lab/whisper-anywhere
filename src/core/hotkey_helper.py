"""The shortcut's low-level hooks, in a helper process of their own.

Windows waits for a low-level hook's answer before a key press or click
reaches any app. In the app, the hooks' Python callbacks also waited for
Python's GIL, which loading or freeing a speech model holds for seconds: the
whole PC froze (v0.2.3). The hooks then stepped aside during loads, so Esc
and the shortcut were dead for those seconds. In their own process they
answer at once, whatever the app is doing.

The helper is this program again, started with HELPER_FLAG. It runs a
HotkeyListener, and the two talk over its stdin and stdout, one JSON array
per line: HotkeyListener calls go in, its signals come back out.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path

import win32api
import win32con
import win32job
from PySide6.QtCore import QCoreApplication, QObject, QProcess, QTimer, Signal, Slot

from core import app_log
from core.hotkey_listener import HotkeyListener, parse_retype_shortcut, parse_shortcut

HELPER_FLAG = "--hotkey-helper"
# What the app may call on the helper's HotkeyListener, and the signals it sends back.
COMMANDS = frozenset(
    {"start", "stop", "rehook", "update_shortcut", "update_retype_shortcut", "set_escape_held_back"}
)
EVENTS = ("pressed", "released", "cancelled", "timed_out", "tapped", "retype_requested", "error", "status", "logged")
# A helper that dies sooner than this after its start is broken, not unlucky:
# it is reported instead of restarted in a loop.
RESTART_AFTER_SECONDS = 10.0


def helper_command() -> list[str]:
    """This program again, as the helper: the exe, or main.py when run from source."""
    if getattr(sys, "frozen", False):
        return [sys.executable, HELPER_FLAG]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"), HELPER_FLAG]


class HotkeyHelper(QObject):
    """The app's side: HotkeyListener's signals and calls, with the listener
    itself running in the helper process."""

    pressed = Signal()
    released = Signal(object)  # the window in front at a real release
    cancelled = Signal()
    timed_out = Signal()  # a hold whose release may have been missed: never type it
    tapped = Signal()  # a mouse trigger let go within TAP_SECONDS: a plain click
    retype_requested = Signal()
    error = Signal(str)
    status = Signal(str)

    def __init__(
        self,
        shortcut: str = "Ctrl+Alt+Q",
        mode: str = "hold",
        command: list[str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        parse_shortcut(shortcut)
        self._command = command or helper_command()
        # What every new helper is told before "start", in this order.
        self._state: dict[str, list] = {
            "update_shortcut": [shortcut, mode],
            "update_retype_shortcut": [""],
            "set_escape_held_back": [False],
        }
        self._process: QProcess | None = None
        self._running = False
        self._holding = False  # a hold the helper's watchdog is timing
        self._started_at = 0.0
        self._job = None  # Windows ends our helpers with this process

    def start(self) -> None:
        self._running = True
        if self._process is None:
            self._launch()

    def stop(self) -> None:
        """Dictation off, or quitting: the helper unhooks and exits."""
        self._running = False
        process, self._process = self._process, None
        if process is not None:
            process.closeWriteChannel()  # it reads the end of its input
            if not process.waitForFinished(2000):
                process.kill()

    def rehook(self) -> None:
        """After unlock and resume: fresh hooks, or a fresh helper if it died."""
        if self._process is not None:
            self._send("rehook")
        elif self._running:
            self._launch()

    def update_shortcut(self, shortcut: str, mode: str) -> None:
        parse_shortcut(shortcut)  # a bad one is refused here, at once
        self._set("update_shortcut", shortcut, mode)

    def update_retype_shortcut(self, shortcut: str) -> None:
        parse_retype_shortcut(shortcut)
        self._set("update_retype_shortcut", shortcut)

    def set_escape_held_back(self, held: bool) -> None:
        self._set("set_escape_held_back", bool(held))

    def _set(self, name: str, *args) -> None:
        self._state[name] = list(args)
        self._send(name, *args)

    def _send(self, name: str, *args) -> None:
        if self._process is not None:
            self._process.write(json.dumps([name, *args]).encode() + b"\n")

    def _launch(self) -> None:
        process = QProcess(self)
        process.readyReadStandardOutput.connect(lambda: self._read(process))
        process.readyReadStandardError.connect(lambda: self._log_errors(process))
        process.finished.connect(lambda: self._exited(process))
        process.start(self._command[0], self._command[1:])
        if not process.waitForStarted(10000):
            # From the event loop: main() starts the helper before the window
            # that shows this error exists.
            message = f"Could not start the global shortcut. {process.errorString()}"
            QTimer.singleShot(0, lambda: self.error.emit(message))
            process.deleteLater()
            return
        self._process, self._started_at = process, time.monotonic()
        self._end_with_this_app(process.processId())
        for name, args in self._state.items():
            self._send(name, *args)
        self._send("start")

    def _read(self, process: QProcess) -> None:
        while process.canReadLine():
            try:
                name, *args = json.loads(bytes(process.readLine()))
            except (ValueError, TypeError):
                continue  # not one of ours
            if name == "logged":
                app_log.log(*args)
            elif name in EVENTS:
                if name in ("pressed", "released", "tapped", "timed_out"):
                    self._holding = name == "pressed" and self._state["update_shortcut"][1] != "toggle"
                getattr(self, name).emit(*args)

    def _log_errors(self, process: QProcess) -> None:
        text = bytes(process.readAllStandardError()).decode(errors="replace").strip()
        if text:
            app_log.log(f"hotkey helper: {text}")

    def _exited(self, process: QProcess) -> None:
        process.deleteLater()
        if process is not self._process:
            return  # stopped on purpose
        self._process = None
        app_log.log(f"hotkey: the helper stopped, exit code {process.exitCode()}")
        if self._holding:  # its watchdog went with it: end the hold, never type it
            self._holding = False
            self.timed_out.emit()
        if not self._running:
            return
        if time.monotonic() - self._started_at >= RESTART_AFTER_SECONDS:
            # A second later: when both processes are being ended (as
            # deploy-update.ps1 does), this one goes before it starts another.
            QTimer.singleShot(1000, self.rehook)
        else:
            self.error.emit("The global shortcut stopped working. Turn Dictation off and on again.")

    def _end_with_this_app(self, pid: int) -> None:
        """Windows ends the helper when this process ends, crash included:
        hooks with no app behind them would keep M3, and Esc while recording,
        from every app. (Its input closing ends it too, but only while it
        still reads.)"""
        try:
            if self._job is None:
                job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
                self._job = job
            helper = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
            win32job.AssignProcessToJobObject(self._job, helper)
            helper.Close()
        except win32api.error as exc:
            app_log.log(f"hotkey: the helper is not tied to the app: {exc}")


class _Commands(QObject):
    """In the helper: runs the app's commands on its main thread."""

    line = Signal(bytes)  # from the reading thread; b"" once the app is gone

    def __init__(self, listener: HotkeyListener, outbox: queue.SimpleQueue) -> None:
        super().__init__()
        self.listener = listener
        self.outbox = outbox
        self.line.connect(self.run)

    @Slot(bytes)
    def run(self, line: bytes) -> None:
        if not line:
            QCoreApplication.quit()
            return
        try:
            name, *args = json.loads(line)
            if name in COMMANDS:
                getattr(self.listener, name)(*args)
        except Exception as exc:
            self.outbox.put(["logged", f"hotkey: a command failed: {exc!r}"])


def _read_commands(commands: _Commands) -> None:
    for line in sys.stdin.buffer:
        commands.line.emit(line)
    commands.line.emit(b"")  # the app closed our input, or is gone


def _send_events(outbox: queue.SimpleQueue) -> None:
    """The listener's signals to the app, from a thread of their own: a hook
    only queues them, and must never wait on a full pipe."""
    try:
        for message in iter(outbox.get, None):
            sys.stdout.buffer.write(json.dumps(message).encode() + b"\n")
            sys.stdout.buffer.flush()
    except OSError:
        pass  # the app is gone


def run_helper() -> int:
    """The helper process: the app's commands in, the listener's signals out."""
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    outbox: queue.SimpleQueue = queue.SimpleQueue()
    listener = HotkeyListener()
    for name in EVENTS:
        getattr(listener, name).connect(lambda *args, name=name: outbox.put([name, *args]))
    commands = _Commands(listener, outbox)
    writer = threading.Thread(target=_send_events, args=(outbox,), daemon=True)
    writer.start()
    threading.Thread(target=_read_commands, args=(commands,), daemon=True).start()
    app.exec()
    listener.stop()  # unhooked before the process goes
    outbox.put(None)
    writer.join(2)
    return 0
