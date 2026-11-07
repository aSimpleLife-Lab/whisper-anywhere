from __future__ import annotations

# ruff: noqa: E402  (the hotkey helper starts before the app's own imports)
import os

# Before anything imports numpy: its OpenBLAS starts a thread per core and
# commits their buffers (23 idle threads, 747 MB); the engines use their own.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import sys

from core.hotkey_helper import HELPER_FLAG, HotkeyHelper, run_helper

if HELPER_FLAG in sys.argv[1:]:
    # This program again, as the shortcut's helper process (core.hotkey_helper):
    # it needs none of the app below.
    raise SystemExit(run_helper())

import multiprocessing
from ctypes import WinDLL, c_wchar_p, get_last_error, windll
from typing import Any, Callable

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from core import app_log
from core.app_version import APP_VERSION
from core.app_icon import app_icon
from core.audio_recorder import AudioRecorder
from core.feedback_player import FeedbackPlayer
from core.hotkey_listener import parse_shortcut, same_shortcut, shortcut_problem
from core.model_manager import ModelManager
from core.settings_manager import SettingsManager
from core.startup_manager import StartupManager
from core.text_inserter import TextInserter
from core.transcriber import Transcriber
from core.tray_manager import TrayManager
from ui.main_window import MainWindow

APP_USER_MODEL_ID = "aSimpleLifeLab.WhisperAnywhere"
DEFAULT_SHORTCUT = "Ctrl+Alt+Q"
DEFAULT_RETYPE_SHORTCUT = "Ctrl+Alt+V"
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
# Where a second copy asks the running one to show its window.
SHOW_SERVER = f"{APP_USER_MODEL_ID}.{os.environ.get('USERNAME', '')}"


def another_copy_running(name: str = APP_USER_MODEL_ID) -> bool:
    """Claims a named mutex that Windows holds until this process exits. Two
    copies would each record and paste every dictation. A copy running as
    administrator owns one this copy may not even open: access denied."""
    kernel32 = WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, f"Local\\{name}")
    return get_last_error() in (ERROR_ALREADY_EXISTS, ERROR_ACCESS_DENIED)


def saved_shortcut(
    settings_manager: SettingsManager, key: str = "shortcut", default: str = DEFAULT_SHORTCUT
) -> str:
    """The saved shortcut, or the default once it no longer works: an
    imported or hand-edited one stopped the app at every sign-in, and one
    with Esc cancelled itself. The retype shortcut is keyboard only."""
    shortcut = str(settings_manager.get(key, default))
    try:
        parsed = parse_shortcut(shortcut)
        usable = not shortcut_problem(shortcut) and not (key == "retype_shortcut" and parsed.mouse_trigger)
    except ValueError:
        usable = False
    if not usable:
        shortcut = default
        settings_manager.set(key, shortcut)
    return shortcut


def saved_retype_shortcut(settings_manager: SettingsManager) -> str:
    """The "type the last dictation again" shortcut, checked the same way; blank is off.
    One that is also the dictation shortcut turns off: a press would do both."""
    if not str(settings_manager.get("retype_shortcut", DEFAULT_RETYPE_SHORTCUT)):
        return ""
    retype = saved_shortcut(settings_manager, "retype_shortcut", DEFAULT_RETYPE_SHORTCUT)
    if same_shortcut(retype, str(settings_manager.get("shortcut", ""))):
        settings_manager.set("retype_shortcut", "")
        return ""
    return retype


def start_up_step(problems: list[str], step: str, action: Callable[[], Any], fallback: Any = None) -> Any:
    """One start-up step: a failure is logged and noted, and the app starts anyway."""
    try:
        return action()
    except Exception as exc:
        app_log.log(f"{step} failed: {type(exc).__name__}")
        problems.append(f"{step}: {exc}")
        return fallback


def set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def should_start_minimized() -> bool:
    flags = {"--minimized", "--hidden", "/minimized", "/tray"}
    if flags.intersection(str(arg).lower() for arg in sys.argv[1:]):
        return True
    get_command_line = windll.kernel32.GetCommandLineW
    get_command_line.restype = c_wchar_p
    command_line = get_command_line() or ""
    return any(flag in command_line.lower() for flag in flags)


def main() -> int:
    multiprocessing.freeze_support()
    set_windows_app_id()
    start_minimized = should_start_minimized()

    app = QApplication(sys.argv)
    app.setApplicationName("Whisper Anywhere")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Whisper Anywhere")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    if another_copy_running():
        if not start_minimized:
            # Opened from the Start menu while it runs in the tray: bring that
            # copy's window forward instead.
            windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            socket = QLocalSocket()
            socket.connectToServer(SHOW_SERVER)
            socket.waitForConnected(1000)
        return 0

    settings_manager = SettingsManager()  # never raises: see load()
    problems = [
        notice
        for notice in (settings_manager.load_notice, settings_manager.model_folder_error)
        if notice
    ]
    start_up_step(  # v0.1 logged what was said
        problems,
        "Removing old logs and recordings",
        lambda: app_log.destroy_legacy_files(settings_manager.config_dir),
    )
    shortcut = start_up_step(
        problems, "Shortcut", lambda: saved_shortcut(settings_manager), DEFAULT_SHORTCUT
    )
    retype_shortcut = start_up_step(
        problems, "Retype shortcut", lambda: saved_retype_shortcut(settings_manager), ""
    )
    if settings_manager.get("debug_logs", False):
        app_log.write_to_disk(settings_manager.debug_log_path)
    app_log.log(f"Whisper Anywhere {APP_VERSION} starting")
    startup_manager = StartupManager()
    startup_state = start_up_step(
        problems,
        "Start with Windows",
        lambda: startup_manager.sync_at_start(bool(settings_manager.get("start_with_windows", False))),
        "unknown",
    )
    app_log.log(f"start with Windows: {startup_state}")
    model_manager = ModelManager(settings_manager)
    audio_recorder = AudioRecorder()
    transcriber = Transcriber(model_manager)
    text_inserter = TextInserter()
    feedback_player = FeedbackPlayer(
        settings_manager.get("start_sound_path", ""),
        settings_manager.get("stop_sound_path", ""),
        volume=float(settings_manager.get("feedback_volume", 0.5)),
    )

    window = MainWindow(
        settings_manager,
        model_manager,
        audio_recorder,
        transcriber,
        text_inserter,
        startup_manager,
    )
    if problems:
        window.set_status("Start-up problem", " ".join(problems))
    tray = TrayManager(model_manager)
    # The keyboard and mouse hooks run in a helper process: a model load
    # holding this one's GIL for seconds can't hold back the PC's input.
    hotkey = HotkeyHelper(shortcut, str(settings_manager.get("shortcut_mode", "hold")))

    hotkey.pressed.connect(window.shortcut_pressed)
    hotkey.released.connect(window.stop_listening_and_transcribe)
    hotkey.tapped.connect(window.drop_tapped_recording)  # a short mouse tap stays a click
    hotkey.timed_out.connect(window.stop_after_timeout)  # a missed release: keep, never type
    hotkey.cancelled.connect(window.cancel_listening)
    hotkey.retype_requested.connect(window.retype_last_dictation)
    window.feedback.connect(feedback_player.play)  # cues follow the microphone, not the key
    window.cue.connect(feedback_player.play_cue)
    window.escape_hold_changed.connect(hotkey.set_escape_held_back)
    window.rehook_requested.connect(hotkey.rehook)
    hotkey.status.connect(window.set_hotkey_status)
    hotkey.error.connect(window.show_hotkey_error)
    window.shortcut_changed.connect(hotkey.update_shortcut)
    window.hotkey_enabled_changed.connect(
        lambda enabled: hotkey.start() if enabled else hotkey.stop()
    )

    def update_retype_shortcut(shortcut: str) -> None:
        try:
            hotkey.update_retype_shortcut(shortcut)
        except ValueError as exc:  # the window checks first; this is the last line
            window.set_status("Shortcut problem", str(exc))

    window.retype_shortcut_changed.connect(update_retype_shortcut)
    update_retype_shortcut(retype_shortcut or "")

    def preview_feedback_volume(volume: float) -> None:
        feedback_player.set_volume(volume)
        feedback_player.play(True)

    window.feedback_volume_changed.connect(preview_feedback_volume)

    tray.start_requested.connect(window.start_listening)
    tray.stop_requested.connect(window.stop_listening_and_transcribe)
    tray.open_requested.connect(window.open_from_tray)
    tray.settings_requested.connect(window.open_settings)
    tray.model_selected.connect(window.select_model)
    window.model_changed.connect(tray.set_current_model)
    window.state_changed.connect(tray.set_state)

    def shutdown() -> None:
        hotkey.stop()
        audio_recorder.close()
        tray.shutdown()

    def request_exit() -> None:
        window._force_exit = True
        window.close()
        app.quit()

    tray.exit_requested.connect(request_exit)
    app.aboutToQuit.connect(shutdown)

    def show_for_second_copy() -> None:
        show_server.nextPendingConnection().deleteLater()
        window.open_from_tray()

    show_server = QLocalServer()
    show_server.newConnection.connect(show_for_second_copy)
    show_server.listen(SHOW_SERVER)

    tray.show()
    hotkey.start()
    if start_minimized:
        window.hide()
        QTimer.singleShot(0, window.hide)
        QTimer.singleShot(1000, window.hide)
    else:
        window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
