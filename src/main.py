from __future__ import annotations

import multiprocessing
import os
import sys
from ctypes import WinDLL, c_wchar_p, get_last_error, windll

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from core import app_log
from core.app_version import APP_VERSION
from core.app_icon import app_icon
from core.audio_recorder import AudioRecorder
from core.feedback_player import FeedbackPlayer
from core.hotkey_listener import HotkeyListener, parse_shortcut
from core.model_manager import ModelManager
from core.settings_manager import SettingsManager
from core.startup_manager import StartupManager, StartupManagerError
from core.text_inserter import TextInserter
from core.transcriber import Transcriber
from core.tray_manager import TrayManager
from ui.main_window import MainWindow

APP_USER_MODEL_ID = "aSimpleLifeLab.WhisperAnywhere"
DEFAULT_SHORTCUT = "Ctrl+Alt+Q"
ERROR_ALREADY_EXISTS = 183
# Where a second copy asks the running one to show its window.
SHOW_SERVER = f"{APP_USER_MODEL_ID}.{os.environ.get('USERNAME', '')}"


def another_copy_running(name: str = APP_USER_MODEL_ID) -> bool:
    """Claims a named mutex that Windows holds until this process exits. Two
    copies would each record and paste every dictation."""
    kernel32 = WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, f"Local\\{name}")
    return get_last_error() == ERROR_ALREADY_EXISTS


def saved_shortcut(settings_manager: SettingsManager) -> str:
    """The saved shortcut, or the default once it no longer parses: an
    imported or hand-edited one stopped the app at every sign-in."""
    shortcut = str(settings_manager.get("shortcut", DEFAULT_SHORTCUT))
    try:
        parse_shortcut(shortcut)
    except ValueError:
        shortcut = DEFAULT_SHORTCUT
        settings_manager.set("shortcut", shortcut)
    return shortcut


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

    settings_manager = SettingsManager()
    app_log.destroy_legacy_files(settings_manager.config_dir)  # v0.1 logged what was said
    shortcut = saved_shortcut(settings_manager)
    if settings_manager.get("debug_logs", False):
        app_log.write_to_disk(settings_manager.debug_log_path)
    app_log.log(f"Whisper Anywhere {APP_VERSION} starting")
    startup_manager = StartupManager()
    startup_sync_error = None
    if settings_manager.get("start_with_windows", False):
        try:
            startup_manager.sync(True)
        except StartupManagerError as exc:
            startup_sync_error = str(exc)
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
    if startup_sync_error:
        window.set_status("Startup setting problem", startup_sync_error)
    tray = TrayManager(model_manager)
    hotkey = HotkeyListener(
        shortcut,
        str(settings_manager.get("shortcut_mode", "hold")),
        start_sound_path=str(settings_manager.get("start_sound_path", "")),
        stop_sound_path=str(settings_manager.get("stop_sound_path", "")),
    )
    transcriber.load_guard = hotkey.paused  # model loads unhook the keyboard and mouse
    text_inserter.input_guard = hotkey.ignoring_injected  # our own keys are not the shortcut

    hotkey.pressed.connect(window.shortcut_pressed)
    hotkey.released.connect(window.stop_listening_and_transcribe)
    hotkey.cancelled.connect(window.cancel_listening)
    window.feedback.connect(feedback_player.play)  # cues follow the microphone, not the key
    hotkey.status.connect(window.set_hotkey_status)
    hotkey.error.connect(window.show_hotkey_error)
    window.shortcut_changed.connect(hotkey.update_shortcut)
    window.hotkey_enabled_changed.connect(
        lambda enabled: hotkey.start() if enabled else hotkey.stop()
    )

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
