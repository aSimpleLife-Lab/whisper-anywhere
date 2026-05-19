from __future__ import annotations

import multiprocessing
import sys

from PySide6.QtWidgets import QApplication

from core.audio_recorder import AudioRecorder
from core.hotkey_listener import HotkeyListener
from core.model_manager import ModelManager
from core.settings_manager import SettingsManager
from core.text_inserter import TextInserter
from core.transcriber import Transcriber
from core.tray_manager import TrayManager
from ui.main_window import MainWindow


def main() -> int:
    multiprocessing.freeze_support()

    app = QApplication(sys.argv)
    app.setApplicationName("Whisper Anywhere")
    app.setOrganizationName("Whisper Anywhere")
    app.setQuitOnLastWindowClosed(False)

    settings_manager = SettingsManager()
    model_manager = ModelManager(settings_manager)
    audio_recorder = AudioRecorder()
    transcriber = Transcriber(model_manager)
    text_inserter = TextInserter()

    window = MainWindow(settings_manager, model_manager, audio_recorder, transcriber, text_inserter)
    tray = TrayManager(model_manager)
    hotkey = HotkeyListener(
        str(settings_manager.get("shortcut", "Ctrl+Alt+Q")),
        str(settings_manager.get("shortcut_mode", "hold")),
        log_path=settings_manager.hotkey_log_path,
        start_sound_path=str(settings_manager.get("start_sound_path", r"C:\Users\Ben\Downloads\startsound.mp3")),
        stop_sound_path=str(settings_manager.get("stop_sound_path", r"C:\Users\Ben\Downloads\stopsound.mp3")),
    )

    hotkey.pressed.connect(window.start_listening)
    hotkey.released.connect(window.stop_listening_and_transcribe)
    hotkey.cancelled.connect(window.cancel_listening)
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
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
