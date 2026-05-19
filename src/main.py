from __future__ import annotations

import multiprocessing
import sys
from ctypes import c_wchar_p, windll

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.app_icon import app_icon
from core.audio_recorder import AudioRecorder
from core.feedback_player import FeedbackPlayer
from core.hotkey_listener import HotkeyListener
from core.model_manager import ModelManager
from core.settings_manager import SettingsManager
from core.startup_manager import StartupManager, StartupManagerError
from core.text_inserter import TextInserter
from core.transcriber import Transcriber
from core.tray_manager import TrayManager
from ui.main_window import MainWindow

APP_USER_MODEL_ID = "aSimpleLifeLab.WhisperAnywhere"


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
    app.setOrganizationName("Whisper Anywhere")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    settings_manager = SettingsManager()
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
    )

    window = MainWindow(settings_manager, model_manager, audio_recorder, transcriber, text_inserter, startup_manager)
    if startup_sync_error:
        window.set_status("Startup setting problem", startup_sync_error)
    tray = TrayManager(model_manager)
    hotkey = HotkeyListener(
        str(settings_manager.get("shortcut", "Ctrl+Alt+Q")),
        str(settings_manager.get("shortcut_mode", "hold")),
        log_path=settings_manager.hotkey_log_path,
        start_sound_path=str(settings_manager.get("start_sound_path", "")),
        stop_sound_path=str(settings_manager.get("stop_sound_path", "")),
    )

    hotkey.pressed.connect(window.start_listening)
    hotkey.released.connect(window.stop_listening_and_transcribe)
    hotkey.cancelled.connect(window.cancel_listening)
    hotkey.feedback.connect(feedback_player.play)
    hotkey.status.connect(window.set_hotkey_status)
    hotkey.error.connect(window.show_hotkey_error)
    window.shortcut_changed.connect(hotkey.update_shortcut)

    tray.start_requested.connect(window.start_listening)
    tray.stop_requested.connect(window.stop_listening_and_transcribe)
    tray.open_requested.connect(window.open_from_tray)
    tray.settings_requested.connect(window.open_from_tray)
    tray.model_selected.connect(window.select_model)
    window.model_changed.connect(tray.set_current_model)

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
