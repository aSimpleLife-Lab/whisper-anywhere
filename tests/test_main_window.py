from __future__ import annotations

from types import SimpleNamespace

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
