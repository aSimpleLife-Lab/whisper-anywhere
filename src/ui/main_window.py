from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.audio_recorder import AudioRecorder, AudioRecorderError
from core.hotkey_listener import parse_shortcut, shortcut_warning
from core.model_manager import ModelManager
from core.settings_manager import SettingsManager
from core.text_inserter import TextInserter, TextInsertionError
from core.transcriber import Transcriber


class MainWindow(QMainWindow):
    model_changed = Signal(str)
    shortcut_changed = Signal(str, str)
    level_changed = Signal(float)
    transcription_done = Signal(str, str)
    transcription_failed = Signal(str, str)
    model_ready = Signal(str)
    model_failed = Signal(str)

    def __init__(
        self,
        settings_manager: SettingsManager,
        model_manager: ModelManager,
        audio_recorder: AudioRecorder,
        transcriber: Transcriber,
        text_inserter: TextInserter,
    ) -> None:
        super().__init__()
        self.settings_manager = settings_manager
        self.model_manager = model_manager
        self.audio_recorder = audio_recorder
        self.transcriber = transcriber
        self.text_inserter = text_inserter
        self._force_exit = False
        self._target_hwnd: int | None = None
        self._is_listening = False
        self._is_transcribing = False
        self._is_preparing_model = False

        self.model_buttons: dict[str, QPushButton] = {}

        self.setWindowTitle("Whisper Anywhere")
        self.setMinimumSize(1060, 720)
        self._build_ui()
        self._connect_signals()
        self._load_settings_into_ui()
        self._apply_styles()
        self.set_status("Ready", "Click anywhere, hold Ctrl + Win, speak, then release.")
        self.prepare_selected_model(auto=True)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(24, 24, 24, 16)
        root_layout.setSpacing(16)

        header = QHBoxLayout()
        title_block = QVBoxLayout()
        self.title_label = QLabel("Whisper Anywhere")
        self.title_label.setObjectName("titleLabel")
        subtitle = QLabel("Speech to Text - Type Anywhere")
        subtitle.setObjectName("subtitleLabel")
        title_block.addWidget(self.title_label)
        title_block.addWidget(subtitle)
        header.addLayout(title_block)
        header.addItem(QSpacerItem(20, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))
        self.settings_path_label = QLabel(f"Settings: {self.settings_manager.settings_path_text}")
        self.settings_path_label.setObjectName("mutedLabel")
        header.addWidget(self.settings_path_label)
        root_layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(16)
        content_layout.setContentsMargins(0, 0, 0, 0)

        content_layout.addWidget(self._build_status_panel())
        content_layout.addWidget(self._build_model_panel())
        content_layout.addWidget(self._build_settings_panel())
        content_layout.addWidget(self._build_how_it_works_panel())
        content_layout.addWidget(self._build_output_panel())
        scroll.setWidget(content)
        root_layout.addWidget(scroll)

        bottom = QHBoxLayout()
        self.bottom_status = QLabel("Ready")
        self.bottom_model = QLabel("Whisper: medium")
        self.bottom_mic = QLabel("Mic: Default system microphone")
        self.bottom_shortcut = QLabel("Shortcut: Ctrl+Win")
        for label in (self.bottom_status, self.bottom_model, self.bottom_mic, self.bottom_shortcut):
            label.setObjectName("bottomLabel")
        bottom.addWidget(self.bottom_status)
        bottom.addStretch(1)
        bottom.addWidget(self.bottom_model)
        bottom.addWidget(self.bottom_mic)
        bottom.addWidget(self.bottom_shortcut)
        root_layout.addLayout(bottom)

        self.setCentralWidget(root)

    def _build_status_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(24)

        self.mic_button = QPushButton("MIC")
        self.mic_button.setObjectName("micButton")
        self.mic_button.setFixedSize(118, 118)
        layout.addWidget(self.mic_button)

        text_block = QVBoxLayout()
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        self.status_detail = QLabel("Hold Ctrl + Win to talk.")
        self.status_detail.setObjectName("mutedLabel")
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setValue(0)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(14)
        self.shortcut_reminder = QLabel("Hold Ctrl + Win to talk")
        self.shortcut_reminder.setObjectName("shortcutReminder")
        text_block.addWidget(self.status_label)
        text_block.addWidget(self.status_detail)
        text_block.addWidget(self.level_bar)
        text_block.addWidget(self.shortcut_reminder)
        layout.addLayout(text_block, 1)

        return panel

    def _build_model_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("Whisper Model")
        title.setObjectName("sectionTitle")
        self.current_model_label = QLabel("Current model: medium")
        self.current_model_label.setObjectName("accentLabel")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.current_model_label)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setSpacing(12)
        for index, info in enumerate(self.model_manager.models()):
            button = QPushButton(
                f"{info.name}\n{info.approximate_size}\n{info.speed} - {info.accuracy}\n{info.recommended_use}"
            )
            button.setCheckable(True)
            button.setObjectName("modelCard")
            button.clicked.connect(lambda checked=False, name=info.name: self.select_model(name))
            self.model_buttons[info.name] = button
            grid.addWidget(button, index // 4, index % 4)
        layout.addLayout(grid)

        model_status_row = QHBoxLayout()
        self.model_status = QLabel("Model setup will run automatically when needed.")
        self.model_status.setObjectName("mutedLabel")
        self.model_progress = QProgressBar()
        self.model_progress.setVisible(False)
        self.model_progress.setFixedWidth(220)
        self.prepare_model_button = QPushButton("Prepare model now")
        model_status_row.addWidget(self.model_status)
        model_status_row.addStretch(1)
        model_status_row.addWidget(self.model_progress)
        model_status_row.addWidget(self.prepare_model_button)
        layout.addLayout(model_status_row)

        return panel

    def _build_settings_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QGridLayout(panel)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(14)

        title = QLabel("V1 Settings")
        title.setObjectName("sectionTitle")
        layout.addWidget(title, 0, 0, 1, 4)

        layout.addWidget(QLabel("Microphone"), 1, 0)
        self.mic_combo = QComboBox()
        layout.addWidget(self.mic_combo, 1, 1, 1, 3)

        layout.addWidget(QLabel("Shortcut"), 2, 0)
        self.shortcut_input = QLineEdit()
        self.shortcut_input.setPlaceholderText("Ctrl+Win")
        layout.addWidget(self.shortcut_input, 2, 1)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Hold to talk", "hold")
        self.mode_combo.addItem("Toggle", "toggle")
        layout.addWidget(self.mode_combo, 2, 2)
        self.apply_shortcut_button = QPushButton("Apply shortcut")
        layout.addWidget(self.apply_shortcut_button, 2, 3)

        self.shortcut_warning = QLabel("")
        self.shortcut_warning.setObjectName("warningLabel")
        layout.addWidget(self.shortcut_warning, 3, 1, 1, 3)

        layout.addWidget(QLabel("Insert method"), 4, 0)
        self.insert_method_combo = QComboBox()
        self.insert_method_combo.addItem("Clipboard paste", "clipboard_paste")
        self.insert_method_combo.addItem("Simulated keystrokes", "simulated_keystrokes")
        layout.addWidget(self.insert_method_combo, 4, 1)

        self.restore_clipboard_checkbox = QCheckBox("Restore previous clipboard after paste")
        layout.addWidget(self.restore_clipboard_checkbox, 4, 2, 1, 2)

        self.auto_download_checkbox = QCheckBox("Download missing Whisper model automatically")
        layout.addWidget(self.auto_download_checkbox, 5, 1, 1, 3)

        return panel

    def _build_how_it_works_panel(self) -> QWidget:
        group = QGroupBox("How it works")
        group.setObjectName("panelGroup")
        layout = QVBoxLayout(group)
        steps = [
            "1. Place your cursor where you want text.",
            "2. Hold Ctrl + Win and speak.",
            "3. Release the shortcut.",
            "4. Whisper Anywhere transcribes locally and types the text there.",
        ]
        for step in steps:
            label = QLabel(step)
            label.setObjectName("stepLabel")
            layout.addWidget(label)
        return group

    def _build_output_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(22, 22, 22, 22)
        title = QLabel("Last transcription")
        title.setObjectName("sectionTitle")
        self.last_transcript = QTextEdit()
        self.last_transcript.setReadOnly(True)
        self.last_transcript.setPlaceholderText("Your most recent transcribed text will appear here.")
        self.last_transcript.setFixedHeight(110)
        layout.addWidget(title)
        layout.addWidget(self.last_transcript)
        return panel

    def _connect_signals(self) -> None:
        self.mic_button.clicked.connect(self.toggle_manual_listening)
        self.prepare_model_button.clicked.connect(lambda: self.prepare_selected_model(auto=False))
        self.apply_shortcut_button.clicked.connect(self.apply_shortcut_settings)
        self.shortcut_input.textChanged.connect(self.update_shortcut_warning)
        self.mic_combo.currentIndexChanged.connect(self.save_microphone_setting)
        self.insert_method_combo.currentIndexChanged.connect(self.save_typing_settings)
        self.restore_clipboard_checkbox.stateChanged.connect(self.save_typing_settings)
        self.auto_download_checkbox.stateChanged.connect(self.save_typing_settings)
        self.level_changed.connect(self.update_level)
        self.transcription_done.connect(self.finish_transcription)
        self.transcription_failed.connect(self.fail_transcription)
        self.model_ready.connect(self.finish_model_prepare)
        self.model_failed.connect(self.fail_model_prepare)

    def _load_settings_into_ui(self) -> None:
        self.refresh_microphones()
        selected = self.model_manager.selected_model()
        self._update_model_buttons(selected)
        self.shortcut_input.setText(str(self.settings_manager.get("shortcut", "Ctrl+Win")))
        mode = str(self.settings_manager.get("shortcut_mode", "hold"))
        self.mode_combo.setCurrentIndex(0 if mode == "hold" else 1)
        insert_method = str(self.settings_manager.get("insert_method", "clipboard_paste"))
        self.insert_method_combo.setCurrentIndex(0 if insert_method == "clipboard_paste" else 1)
        self.restore_clipboard_checkbox.setChecked(bool(self.settings_manager.get("restore_clipboard", True)))
        self.auto_download_checkbox.setChecked(bool(self.settings_manager.get("auto_download_model", True)))
        self.update_shortcut_warning()
        self.update_bottom_labels()

    def refresh_microphones(self) -> None:
        current = str(self.settings_manager.get("microphone_device", "default"))
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        for device in self.audio_recorder.list_input_devices():
            self.mic_combo.addItem(device.name, device.id)
        index = self.mic_combo.findData(current)
        self.mic_combo.setCurrentIndex(index if index >= 0 else 0)
        self.mic_combo.blockSignals(False)

    def select_model(self, model_name: str) -> None:
        self.model_manager.select_model(model_name)
        self._update_model_buttons(model_name)
        self.update_bottom_labels()
        self.model_changed.emit(model_name)
        self.prepare_selected_model(auto=True)

    def _update_model_buttons(self, selected: str) -> None:
        for name, button in self.model_buttons.items():
            button.setChecked(name == selected)
        self.current_model_label.setText(f"Current model: {selected}")

    def prepare_selected_model(self, auto: bool) -> None:
        if self._is_preparing_model:
            return
        if self.model_manager.is_model_ready():
            self.model_status.setText(f"Model ready: {self.model_manager.selected_model()}")
            return
        if auto and not bool(self.settings_manager.get("auto_download_model", True)):
            self.model_status.setText("Selected model is not prepared yet. Click Prepare model now.")
            return
        if not auto and not bool(self.settings_manager.get("auto_download_model", True)):
            answer = QMessageBox.question(
                self,
                "Download Whisper model?",
                "The selected Whisper model is missing. Download it now so voice typing can work?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._is_preparing_model = True
        self.model_progress.setVisible(True)
        self.model_progress.setRange(0, 0)
        self.model_status.setText(f"Preparing {self.model_manager.selected_model()} model. This can take a while the first time.")
        settings = self.settings_manager.all()
        threading.Thread(target=self._prepare_model_worker, args=(settings,), daemon=True).start()

    def _prepare_model_worker(self, settings: dict[str, Any]) -> None:
        try:
            self.transcriber.prepare_selected_model(settings)
        except Exception as exc:
            self.model_failed.emit(str(exc))
        else:
            self.model_ready.emit(self.model_manager.selected_model())

    def finish_model_prepare(self, model_name: str) -> None:
        self._is_preparing_model = False
        self.model_progress.setVisible(False)
        self.model_status.setText(f"Model ready: {model_name}")
        self.set_status("Ready", "Click anywhere, hold Ctrl + Win, speak, then release.")

    def fail_model_prepare(self, message: str) -> None:
        self._is_preparing_model = False
        self.model_progress.setVisible(False)
        self.model_status.setText(message)
        self.set_status("Error", message)

    def toggle_manual_listening(self) -> None:
        if self._is_listening:
            self.stop_listening_and_transcribe()
        else:
            self.start_listening()

    def start_listening(self) -> None:
        if self._is_listening or self._is_transcribing:
            return
        if self._is_preparing_model:
            self.set_status("Preparing", "The Whisper model is still being prepared. Try again when it says Ready.")
            return
        if not self.model_manager.is_model_ready():
            self.prepare_selected_model(auto=True)
            self.set_status("Preparing", "Preparing the selected Whisper model first.")
            return

        microphone_id = self.mic_combo.currentData() or "default"
        self._target_hwnd = self.text_inserter.get_foreground_window()
        try:
            self.audio_recorder.start(str(microphone_id), self.level_changed.emit)
        except AudioRecorderError as exc:
            self.set_status("Error", str(exc))
            return

        self._is_listening = True
        self.mic_button.setText("STOP")
        self.set_status("Listening", "Speak clearly. Release the shortcut to transcribe and type.")

    def stop_listening_and_transcribe(self) -> None:
        if not self._is_listening:
            return
        self._is_listening = False
        self.mic_button.setText("MIC")
        self.level_bar.setValue(0)

        try:
            audio_path = self.audio_recorder.stop()
        except AudioRecorderError as exc:
            self.set_status("Error", str(exc))
            return

        self._is_transcribing = True
        self.set_status("Transcribing", "Local Whisper is turning your speech into text.")
        settings = self.settings_manager.all()
        threading.Thread(target=self._transcribe_worker, args=(audio_path, settings), daemon=True).start()

    def cancel_listening(self) -> None:
        if not self._is_listening:
            return
        self._is_listening = False
        self.mic_button.setText("MIC")
        self.audio_recorder.cancel()
        self.level_bar.setValue(0)
        self.set_status("Ready", "Recording cancelled.")

    def _transcribe_worker(self, audio_path: str, settings: dict[str, Any]) -> None:
        try:
            text = self.transcriber.transcribe(audio_path, settings)
        except Exception as exc:
            self.transcription_failed.emit(str(exc), audio_path)
        else:
            self.transcription_done.emit(text, audio_path)

    def finish_transcription(self, text: str, audio_path: str) -> None:
        self._is_transcribing = False
        self._delete_temp_audio(audio_path)
        self.last_transcript.setPlainText(text)
        if not text:
            self.set_status("Ready", "No speech was detected. Try speaking closer to the microphone.")
            return

        self.set_status("Typing", "Typing into the focused Windows app.")
        try:
            self.text_inserter.insert_text(text, self._target_hwnd, self.settings_manager.all())
        except TextInsertionError as exc:
            self.set_status("Error", str(exc))
            return
        self.set_status("Ready", "Done. Click anywhere and use the shortcut again.")

    def fail_transcription(self, message: str, audio_path: str) -> None:
        self._is_transcribing = False
        self._delete_temp_audio(audio_path)
        self.set_status("Error", message)

    def _delete_temp_audio(self, audio_path: str) -> None:
        if not bool(self.settings_manager.get("delete_temp_audio", True)):
            return
        try:
            Path(audio_path).unlink(missing_ok=True)
        except OSError:
            pass

    def apply_shortcut_settings(self) -> None:
        shortcut = self.shortcut_input.text().strip() or "Ctrl+Win"
        try:
            parse_shortcut(shortcut)
        except ValueError as exc:
            QMessageBox.warning(self, "Shortcut problem", str(exc))
            return

        warning = shortcut_warning(shortcut)
        if warning and not warning.startswith("Default V1"):
            QMessageBox.warning(self, "Shortcut warning", warning)
        mode = str(self.mode_combo.currentData() or "hold")
        self.shortcut_changed.emit(shortcut, mode)
        self.settings_manager.update({"shortcut": shortcut, "shortcut_mode": mode})
        self.shortcut_reminder.setText(f"{self._mode_label()} {shortcut} to talk")
        self.update_bottom_labels()
        self.set_status("Ready", f"Shortcut saved: {shortcut}")

    def save_microphone_setting(self) -> None:
        value = self.mic_combo.currentData() or "default"
        self.settings_manager.set("microphone_device", str(value))
        self.update_bottom_labels()

    def save_typing_settings(self) -> None:
        self.settings_manager.update(
            {
                "insert_method": str(self.insert_method_combo.currentData() or "clipboard_paste"),
                "restore_clipboard": self.restore_clipboard_checkbox.isChecked(),
                "auto_download_model": self.auto_download_checkbox.isChecked(),
            }
        )
        if self.auto_download_checkbox.isChecked():
            self.prepare_selected_model(auto=True)

    def update_shortcut_warning(self) -> None:
        warning = shortcut_warning(self.shortcut_input.text().strip() or "Ctrl+Win")
        self.shortcut_warning.setText(warning)

    def update_level(self, level: float) -> None:
        self.level_bar.setValue(int(level * 100))

    def set_status(self, status: str, detail: str) -> None:
        self.status_label.setText(status)
        self.status_detail.setText(detail)
        self.bottom_status.setText(status)

    def update_bottom_labels(self) -> None:
        model = self.model_manager.selected_model()
        mic_name = self.mic_combo.currentText() or "Default system microphone"
        shortcut = str(self.settings_manager.get("shortcut", "Ctrl+Win"))
        self.bottom_model.setText(f"Whisper: {model}")
        self.bottom_mic.setText(f"Mic: {mic_name}")
        self.bottom_shortcut.setText(f"Shortcut: {shortcut}")
        self.shortcut_reminder.setText(f"{self._mode_label()} {shortcut} to talk")

    def _mode_label(self) -> str:
        return "Press" if str(self.settings_manager.get("shortcut_mode", "hold")) == "toggle" else "Hold"

    def open_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def exit_app(self) -> None:
        self._force_exit = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if bool(self.settings_manager.get("minimize_to_tray", True)) and not self._force_exit:
            event.ignore()
            self.hide()
            return
        self.audio_recorder.close()
        event.accept()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #0f1117; color: #f5f7fb; font-size: 14px; }
            #titleLabel { font-size: 26px; font-weight: 800; }
            #subtitleLabel, #mutedLabel, #bottomLabel { color: #a8b0c2; }
            #panel, #panelGroup { background: #151922; border: 1px solid #283041; border-radius: 10px; }
            #sectionTitle { font-size: 18px; font-weight: 800; }
            #statusLabel { font-size: 24px; font-weight: 800; color: #a78bfa; }
            #accentLabel { color: #a78bfa; font-weight: 800; }
            #warningLabel { color: #fbbf24; }
            #shortcutReminder { color: #d7dded; background: #111827; border: 1px solid #334155; border-radius: 8px; padding: 8px; }
            #micButton { border: 2px solid #8b5cf6; border-radius: 59px; background: #241447; color: white; font-size: 24px; font-weight: 800; }
            #micButton:hover { background: #31205d; }
            QPushButton { background: #1f2937; border: 1px solid #374151; border-radius: 8px; padding: 9px 12px; color: #f8fafc; }
            QPushButton:hover { border-color: #8b5cf6; }
            QPushButton:checked, #modelCard:checked { background: #2e235f; border: 1px solid #8b5cf6; color: white; }
            #modelCard { min-height: 116px; text-align: left; font-weight: 700; }
            QLineEdit, QComboBox, QTextEdit { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 8px; color: #f8fafc; }
            QProgressBar { background: #111827; border: 1px solid #334155; border-radius: 7px; }
            QProgressBar::chunk { background: #8b5cf6; border-radius: 7px; }
            QCheckBox { spacing: 8px; }
            QGroupBox { font-weight: 800; margin-top: 12px; padding: 14px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
            """
        )
