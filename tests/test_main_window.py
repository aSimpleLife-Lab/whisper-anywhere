from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import ui.main_window as mw


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value) -> None:
        self[key] = value


def test_a_rebuilt_transcriber_keeps_the_input_freeze_guard() -> None:
    # Model and Performance changes rebuild the transcriber; the rebuilt one
    # loaded Voz with the hooks live and froze the PC for ~3 s per dictation.
    guard = object()
    old = mw.Transcriber(SimpleNamespace())
    old.load_guard = guard
    window = SimpleNamespace(_warm_up_generation=0, transcriber=old, model_manager=old.model_manager)

    mw.MainWindow._reset_transcriber(window)

    assert window.transcriber is not old
    assert window.transcriber.load_guard is guard


def test_toggle_shortcut_starts_or_stops_by_the_windows_own_state() -> None:
    calls: list[str] = []
    window = SimpleNamespace(
        _is_listening=False,
        settings_manager=FakeSettings(shortcut_mode="toggle"),
        start_listening=lambda: calls.append("start"),
        stop_listening_and_transcribe=lambda: calls.append("stop"),
    )

    mw.MainWindow.shortcut_pressed(window)  # refused: still not listening...
    mw.MainWindow.shortcut_pressed(window)  # ...so this press starts again
    window._is_listening = True
    mw.MainWindow.shortcut_pressed(window)
    window.settings_manager["shortcut_mode"] = "hold"
    mw.MainWindow.shortcut_pressed(window)  # hold mode: the release stops it

    assert calls == ["start", "start", "stop", "start"]


def test_copy_keeps_the_dictation_out_of_clipboard_history() -> None:
    copied: list[tuple[str, bool]] = []
    window = SimpleNamespace(
        last_transcript=SimpleNamespace(toPlainText=lambda: "secret words"),
        text_inserter=SimpleNamespace(
            set_clipboard_text=lambda text, private=False: copied.append((text, private))
        ),
        set_status=lambda status, detail: None,
    )

    mw.MainWindow.copy_transcript(window)

    assert copied == [("secret words", True)]


def test_ctrl_c_in_the_transcript_box_is_private_too() -> None:
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    box = mw._PrivateTextEdit()
    box.setPlainText("secret words")
    box.selectAll()

    mime = box.createMimeDataFromSelection()

    assert mime.text() == "secret words"
    history = 'application/x-qt-windows-mime;value="CanIncludeInClipboardHistory"'
    assert bytes(mime.data(history)) == b"\0\0\0\0"


class FakeCombo:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.index = -1

    def blockSignals(self, blocked: bool) -> bool:
        return False

    def clear(self) -> None:
        self.items = []

    def addItem(self, name: str, data: str) -> None:
        self.items.append((name, data))

    def findData(self, data: str) -> int:
        return next((i for i, item in enumerate(self.items) if item[1] == data), -1)

    def setCurrentIndex(self, index: int) -> None:
        self.index = index


def test_a_missing_microphone_stays_the_saved_choice() -> None:
    # A headset that connects after logon was replaced by "default" for good,
    # and the frozen device list never showed it even after a refresh.
    settings = FakeSettings(microphone_device="7", microphone_name="Headset")
    rescans: list[bool] = []
    window = SimpleNamespace(
        settings_manager=settings,
        mic_combo=FakeCombo(),
        audio_recorder=SimpleNamespace(
            rescan=lambda: rescans.append(True),
            list_input_devices=lambda keep=None: [SimpleNamespace(id="default", name="Default")],
        ),
        _resolve_saved_microphone=lambda: "",
        _append_activity_log=lambda message: None,
        set_status=lambda status, detail: None,
        update_bottom_labels=lambda: None,
    )

    mw.MainWindow.refresh_microphones(window, rescan=True)

    assert rescans == [True]
    assert settings["microphone_device"] == "7"
    assert window.mic_combo.index == 0  # the default is used meanwhile


def test_a_download_still_arriving_is_not_called_too_slow(tmp_path) -> None:
    # A fixed 15-minute limit stopped slow but working downloads, and the next
    # try wrote the same file alongside them. Only a stall is a failure now.
    (tmp_path / "encoder.onnx.data.part").write_bytes(b"x" * 200)
    rearmed: list[int] = []
    ended: list[bool] = []
    window = SimpleNamespace(
        _active_model_prepare_id=1,
        _is_preparing_model=True,
        _model_prepare_bytes=100,
        model_manager=SimpleNamespace(model_storage_path=lambda: tmp_path),
        _start_model_prepare_timer=rearmed.append,
        _end_model_prepare=lambda: ended.append(True),
        _reset_transcriber=lambda: None,
        _append_activity_log=lambda message: None,
        _refresh_model_status=lambda: None,
        model_status=SimpleNamespace(setText=lambda text: None),
        set_status=lambda status, detail: None,
    )

    mw.MainWindow.handle_model_prepare_timeout(window, 1)
    assert (rearmed, ended) == ([1], [])

    window._model_prepare_bytes = 200  # nothing new since the timer was set
    mw.MainWindow.handle_model_prepare_timeout(window, 1)
    assert ended == [True]


def test_loading_settings_into_the_window_turns_the_shortcut_back_on() -> None:
    # Import and "Run setup again" show Dictation on, but a shortcut the user
    # had paused stayed off.
    window = MagicMock()

    mw.MainWindow._load_settings_into_ui(window)

    window.hotkey_enabled_changed.emit.assert_called_once_with(True)


def test_setup_keeps_the_chosen_microphone_and_toggle_mode(monkeypatch) -> None:
    # Setup saved only the device number, so the old microphone's name won it
    # straight back; it also switched a toggle user back to hold.
    class FakeDialog:
        def __init__(self, parent) -> None:
            self.shortcut_input = SimpleNamespace(text=lambda: "M3")
            self.model_combo = SimpleNamespace(currentData=lambda: "voz")
            self.mic_combo = SimpleNamespace(
                currentData=lambda: "2", currentText=lambda: "Blue Yeti"
            )
            self.device_combo = SimpleNamespace(currentData=lambda: "cpu")

        def exec(self):
            return mw.QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw, "FirstRunSetupDialog", FakeDialog)
    settings = FakeSettings(
        microphone_device="1",
        microphone_name="Jabra",
        shortcut_mode="toggle",
        device="cpu",
        compute_type="int8",
    )
    window = MagicMock()
    window.settings_manager = settings

    mw.MainWindow.show_first_run_setup(window, force=True)

    assert (settings["microphone_device"], settings["microphone_name"]) == ("2", "Blue Yeti")
    assert settings["shortcut_mode"] == "toggle"
    assert settings["compute_type"] == "int8"  # same hardware, same precision
    window.shortcut_changed.emit.assert_called_once_with("M3", "toggle")


def test_importing_settings_switches_the_debug_log_to_match(monkeypatch, tmp_path) -> None:
    # The import showed the debug log off while it kept writing to disk.
    written: list[object] = []
    shown: list[str] = []
    monkeypatch.setattr(mw.app_log, "write_to_disk", written.append)
    monkeypatch.setattr(mw, "show_message", lambda parent, title, text, *args: shown.append(text))
    exported = tmp_path / "settings.json"
    exported.write_text('{"debug_logs": false}', encoding="utf-8-sig")  # PowerShell 5.1 adds a BOM
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *args: (str(exported), ""))

    class ImportingSettings(FakeSettings):
        debug_log_path = tmp_path / "debug.log"

        def import_portable(self, values):
            self.update(values)
            return []

    window = MagicMock()
    window.settings_manager = ImportingSettings(debug_logs=True)

    mw.MainWindow.import_settings(window)

    assert shown == []
    assert written == [None]


def test_old_thread_counts_still_show_in_the_menu() -> None:
    # v0.1 offered 1 thread and counts above this PC's cores; the menu showed
    # Auto while the engine kept using the old count.
    threads = FakeCombo()
    threads.addItem("Auto", "auto")
    threads.addItem("2 threads", 2)
    window = MagicMock()
    window.cpu_threads_combo = threads
    window.compute_combo = FakeCombo()
    window.settings_manager = FakeSettings(cpu_threads=1)
    window._select_combo_data = lambda combo, value: mw.MainWindow._select_combo_data(
        window, combo, value
    )

    mw.MainWindow._restore_performance_controls(window)

    assert threads.items[threads.index] == ("1 thread", 1)
