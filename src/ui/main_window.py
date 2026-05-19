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
from core.model_manager import LARGE_MODELS, ModelManager, WhisperModelInfo
from core.settings_manager import SettingsManager
from core.text_inserter import TextInserter, TextInsertionError, TextTarget
from core.transcriber import Transcriber

TRANSCRIPTION_TIMEOUT_SECONDS = 90.0


class MainWindow(QMainWindow):
    model_changed = Signal(str)
    shortcut_changed = Signal(str, str)
    level_changed = Signal(float)
    transcription_done = Signal(int, str, str)
    transcription_failed = Signal(int, str, str)
    transcription_timeout = Signal(int, str)
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
        self._target_hwnd: TextTarget | None = None
        self._is_listening = False
        self._is_transcribing = False
        self._is_preparing_model = False
        self._loading_ui = False
        self._transcription_request_id = 0
        self._active_transcription_id = 0
        self._transcription_timer: threading.Timer | None = None

        self.model_buttons: dict[str, QPushButton] = {}

        self.setWindowTitle("Whisper Anywhere")
        self.setMinimumSize(1080, 780)
        self._build_ui()
        self._connect_signals()
        self._load_settings_into_ui()
        self._apply_styles()
        shortcut = str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q"))
        self.set_status("Ready", f"Click anywhere, {self._mode_label().lower()} {shortcut}, speak, then release.")
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
        content_layout.addWidget(self._build_performance_panel())
        content_layout.addWidget(self._build_how_it_works_panel())
        content_layout.addWidget(self._build_output_panel())
        scroll.setWidget(content)
        root_layout.addWidget(scroll)

        bottom = QHBoxLayout()
        self.bottom_status = QLabel("Ready")
        self.bottom_model = QLabel("Whisper: base")
        self.bottom_mic = QLabel("Mic: Default system microphone")
        self.bottom_shortcut = QLabel("Shortcut: Ctrl+Alt+Q")
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
        self.status_detail = QLabel("Hold Ctrl + Alt + Q to talk.")
        self.status_detail.setObjectName("mutedLabel")
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setValue(0)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(14)
        self.shortcut_reminder = QLabel("Hold Ctrl + Alt + Q to talk")
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
        self.current_model_label = QLabel("Current model: base")
        self.current_model_label.setObjectName("accentLabel")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.current_model_label)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setSpacing(12)
        for index, info in enumerate(self.model_manager.models()):
            button = QPushButton(self._model_card_text(info))
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

        title = QLabel("Core Settings")
        title.setObjectName("sectionTitle")
        layout.addWidget(title, 0, 0, 1, 4)

        layout.addWidget(QLabel("Microphone"), 1, 0)
        self.mic_combo = QComboBox()
        layout.addWidget(self.mic_combo, 1, 1, 1, 3)

        layout.addWidget(QLabel("Shortcut"), 2, 0)
        self.shortcut_input = QLineEdit()
        self.shortcut_input.setPlaceholderText("Ctrl+Alt+Q")
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

        layout.addWidget(QLabel("Hotkey status"), 4, 0)
        self.hotkey_status_label = QLabel("Starting global shortcut listener.")
        self.hotkey_status_label.setObjectName("mutedLabel")
        layout.addWidget(self.hotkey_status_label, 4, 1, 1, 3)

        self.hotkey_log_label = QLabel(f"Hotkey log: {self.settings_manager.hotkey_log_path_text}")
        self.hotkey_log_label.setObjectName("mutedLabel")
        layout.addWidget(self.hotkey_log_label, 5, 1, 1, 3)

        layout.addWidget(QLabel("Insert method"), 6, 0)
        self.insert_method_combo = QComboBox()
        self.insert_method_combo.addItem("Clipboard paste", "clipboard_paste")
        layout.addWidget(self.insert_method_combo, 6, 1)

        self.restore_clipboard_checkbox = QCheckBox("Restore previous clipboard after paste")
        layout.addWidget(self.restore_clipboard_checkbox, 6, 2, 1, 2)

        return panel

    def _build_performance_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QGridLayout(panel)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(14)

        title = QLabel("Performance / Hardware")
        title.setObjectName("sectionTitle")
        layout.addWidget(title, 0, 0, 1, 4)

        layout.addWidget(QLabel("Performance Mode"), 1, 0)
        self.performance_combo = QComboBox()
        self.performance_combo.addItem("Fast", "fast")
        self.performance_combo.addItem("Balanced", "balanced")
        self.performance_combo.addItem("Accurate", "accurate")
        self.performance_combo.addItem("Low RAM Mode", "low_ram")
        self.performance_combo.addItem("Low VRAM Mode", "low_vram")
        layout.addWidget(self.performance_combo, 1, 1)

        layout.addWidget(QLabel("Hardware"), 1, 2)
        self.device_combo = QComboBox()
        self.device_combo.addItem("Auto", "auto")
        self.device_combo.addItem("CPU Only", "cpu")
        self.device_combo.addItem("GPU Preferred", "gpu")
        layout.addWidget(self.device_combo, 1, 3)

        self.low_ram_checkbox = QCheckBox("Low RAM Mode")
        self.low_vram_checkbox = QCheckBox("Low VRAM Mode")
        self.fallback_cpu_checkbox = QCheckBox("Fall back to CPU if GPU fails")
        layout.addWidget(self.low_ram_checkbox, 2, 1)
        layout.addWidget(self.low_vram_checkbox, 2, 2)
        layout.addWidget(self.fallback_cpu_checkbox, 2, 3)

        self.warn_large_checkbox = QCheckBox("Warn before loading large models")
        self.auto_download_checkbox = QCheckBox("Auto-download missing models")
        self.use_gpu_checkbox = QCheckBox("Use GPU if available")
        layout.addWidget(self.warn_large_checkbox, 3, 1)
        layout.addWidget(self.auto_download_checkbox, 3, 2)
        layout.addWidget(self.use_gpu_checkbox, 3, 3)

        layout.addWidget(QLabel("Advanced"), 4, 0)
        self.compute_combo = QComboBox()
        self.compute_combo.addItem("Auto", "auto")
        self.compute_combo.addItem("int8", "int8")
        self.compute_combo.addItem("int8_float16", "int8_float16")
        self.compute_combo.addItem("float16", "float16")
        self.compute_combo.addItem("float32", "float32")
        layout.addWidget(self.compute_combo, 4, 1)

        self.cpu_threads_combo = QComboBox()
        self.cpu_threads_combo.addItem("CPU threads: Auto", "auto")
        for value in (1, 2, 4, 6, 8, 12, 16, 24, 32):
            self.cpu_threads_combo.addItem(f"CPU threads: {value}", value)
        layout.addWidget(self.cpu_threads_combo, 4, 2)

        self.hardware_note = QLabel("Auto uses GPU when a compatible CUDA setup is available, otherwise CPU.")
        self.hardware_note.setObjectName("mutedLabel")
        layout.addWidget(self.hardware_note, 5, 1, 1, 3)

        return panel

    def _build_how_it_works_panel(self) -> QWidget:
        group = QGroupBox("How it works")
        group.setObjectName("panelGroup")
        layout = QVBoxLayout(group)
        steps = [
            "1. Place your cursor where you want text.",
            "2. Hold Ctrl + Alt + Q and speak.",
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
        self.mic_button.clicked.connect(lambda checked=False: self.toggle_manual_listening())
        self.prepare_model_button.clicked.connect(lambda checked=False: self.prepare_selected_model(auto=False))
        self.apply_shortcut_button.clicked.connect(lambda checked=False: self.apply_shortcut_settings())
        self.shortcut_input.textChanged.connect(lambda text="": self.update_shortcut_warning())
        self.mic_combo.currentIndexChanged.connect(lambda index=0: self.save_microphone_setting())
        self.insert_method_combo.currentIndexChanged.connect(lambda index=0: self.save_typing_settings())
        self.restore_clipboard_checkbox.stateChanged.connect(lambda state=0: self.save_typing_settings())
        self.performance_combo.currentIndexChanged.connect(lambda index=0: self.apply_performance_preset())
        self.device_combo.currentIndexChanged.connect(lambda index=0: self.save_performance_settings())
        self.compute_combo.currentIndexChanged.connect(lambda index=0: self.save_performance_settings())
        self.cpu_threads_combo.currentIndexChanged.connect(lambda index=0: self.save_performance_settings())
        self.low_ram_checkbox.stateChanged.connect(lambda state=0: self.save_performance_settings())
        self.low_vram_checkbox.stateChanged.connect(lambda state=0: self.save_performance_settings())
        self.fallback_cpu_checkbox.stateChanged.connect(lambda state=0: self.save_performance_settings())
        self.warn_large_checkbox.stateChanged.connect(lambda state=0: self.save_performance_settings())
        self.auto_download_checkbox.stateChanged.connect(lambda state=0: self.save_performance_settings())
        self.use_gpu_checkbox.stateChanged.connect(lambda state=0: self.save_performance_settings())
        self.level_changed.connect(self.update_level)
        self.transcription_done.connect(self.finish_transcription)
        self.transcription_failed.connect(self.fail_transcription)
        self.transcription_timeout.connect(self.handle_transcription_timeout)
        self.model_ready.connect(self.finish_model_prepare)
        self.model_failed.connect(self.fail_model_prepare)

    def _load_settings_into_ui(self) -> None:
        self._loading_ui = True
        self.refresh_microphones()
        selected = self.model_manager.selected_model()
        self._update_model_buttons(selected)
        self.shortcut_input.setText(str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q")))
        mode = str(self.settings_manager.get("shortcut_mode", "hold"))
        self.mode_combo.setCurrentIndex(0 if mode == "hold" else 1)
        self.insert_method_combo.setCurrentIndex(0)
        self.restore_clipboard_checkbox.setChecked(bool(self.settings_manager.get("restore_clipboard", True)))
        self._select_combo_data(self.performance_combo, self.settings_manager.get("performance_preset", "balanced"))
        self._select_combo_data(self.device_combo, self.settings_manager.get("device", "auto"))
        self._select_combo_data(self.compute_combo, self.settings_manager.get("compute_type", "auto"))
        self._select_combo_data(self.cpu_threads_combo, self.settings_manager.get("cpu_threads", "auto"))
        self.low_ram_checkbox.setChecked(bool(self.settings_manager.get("low_ram_mode", False)))
        self.low_vram_checkbox.setChecked(bool(self.settings_manager.get("low_vram_mode", False)))
        self.fallback_cpu_checkbox.setChecked(bool(self.settings_manager.get("fallback_to_cpu", True)))
        self.warn_large_checkbox.setChecked(bool(self.settings_manager.get("warn_before_large_models", True)))
        self.auto_download_checkbox.setChecked(bool(self.settings_manager.get("auto_download_models", True)))
        self.use_gpu_checkbox.setChecked(bool(self.settings_manager.get("use_gpu_if_available", True)))
        self._loading_ui = False
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

    def _pipeline_busy(self) -> bool:
        return self._is_listening or self._is_transcribing or self._is_preparing_model

    def _pipeline_busy_message(self) -> str:
        return "Wait until the current recording, transcription, or model preparation finishes before changing models or performance settings."

    def _restore_performance_controls(self) -> None:
        self._loading_ui = True
        try:
            self._select_combo_data(self.performance_combo, self.settings_manager.get("performance_preset", "balanced"))
            self._select_combo_data(self.device_combo, self.settings_manager.get("device", "auto"))
            self._select_combo_data(self.compute_combo, self.settings_manager.get("compute_type", "auto"))
            self._select_combo_data(self.cpu_threads_combo, self.settings_manager.get("cpu_threads", "auto"))
            self.low_ram_checkbox.setChecked(bool(self.settings_manager.get("low_ram_mode", False)))
            self.low_vram_checkbox.setChecked(bool(self.settings_manager.get("low_vram_mode", False)))
            self.fallback_cpu_checkbox.setChecked(bool(self.settings_manager.get("fallback_to_cpu", True)))
            self.warn_large_checkbox.setChecked(bool(self.settings_manager.get("warn_before_large_models", True)))
            self.auto_download_checkbox.setChecked(bool(self.settings_manager.get("auto_download_models", True)))
            self.use_gpu_checkbox.setChecked(bool(self.settings_manager.get("use_gpu_if_available", True)))
        finally:
            self._loading_ui = False

    def _cancel_transcription_timer(self) -> None:
        if self._transcription_timer is not None:
            self._transcription_timer.cancel()
            self._transcription_timer = None

    def _start_transcription_timer(self, request_id: int, audio_path: str) -> None:
        self._cancel_transcription_timer()
        timer = threading.Timer(
            TRANSCRIPTION_TIMEOUT_SECONDS,
            lambda: self.transcription_timeout.emit(request_id, audio_path),
        )
        timer.daemon = True
        self._transcription_timer = timer
        timer.start()

    def _reset_transcriber(self) -> None:
        self.transcriber = Transcriber(self.model_manager)

    def select_model(self, model_name: str) -> None:
        if self._pipeline_busy():
            self._update_model_buttons(self.model_manager.selected_model())
            self.set_status("Busy", self._pipeline_busy_message())
            return
        normalized = self.model_manager.normalize_model_name(model_name)
        if normalized != self.model_manager.selected_model() and not self._confirm_model_choice(normalized):
            self._update_model_buttons(self.model_manager.selected_model())
            return
        self.model_manager.select_model(normalized)
        self._reset_transcriber()
        self._update_model_buttons(normalized)
        self.update_bottom_labels()
        self.model_changed.emit(normalized)
        self.prepare_selected_model(auto=True)

    def _confirm_model_choice(self, model_name: str) -> bool:
        warnings: list[str] = []
        if self.warn_large_checkbox.isChecked() and model_name in LARGE_MODELS:
            warnings.append("Large Whisper models need much more RAM or VRAM and can take longer to load.")
        if self.low_ram_checkbox.isChecked() and model_name in LARGE_MODELS:
            warnings.append("Low RAM Mode is on, so a smaller model such as base or small is safer.")
        if self.low_vram_checkbox.isChecked() and model_name in {"large", "large-v2", "large-v3"}:
            warnings.append("Low VRAM Mode is on, so large-v3 may be slow or fail on smaller GPUs.")
        if not warnings:
            return True
        answer = QMessageBox.question(
            self,
            "Load larger model?",
            "\n\n".join(warnings) + "\n\nContinue with this model?",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _update_model_buttons(self, selected: str) -> None:
        for info in self.model_manager.models():
            button = self.model_buttons.get(info.name)
            if button is None:
                continue
            button.setText(self._model_card_text(info))
            button.setChecked(info.name == selected)
        self.current_model_label.setText(f"Current model: {selected}")

    def _model_card_text(self, info: WhisperModelInfo) -> str:
        status = self.model_manager.model_status_label(info.name)
        return (
            f"{info.name} - {status}\n"
            f"{info.approximate_size}\n"
            f"Speed: {info.speed_estimate} | Accuracy: {info.accuracy_estimate}\n"
            f"Resources: {info.resource_usage_estimate}\n"
            f"Use: {info.recommended_use}"
        )

    def prepare_selected_model(self, auto: bool) -> None:
        if self._is_preparing_model:
            return
        if self._is_listening or self._is_transcribing:
            if not auto:
                self.set_status("Busy", self._pipeline_busy_message())
            return
        selected = self.model_manager.selected_model()
        if self.model_manager.is_model_ready():
            self.model_status.setText(f"Model ready: {selected}")
            self._update_model_buttons(selected)
            return
        if auto and not bool(self.settings_manager.get("auto_download_models", True)):
            self.model_status.setText("Selected model is not installed yet. Click Prepare model now.")
            self._update_model_buttons(selected)
            return
        if not auto and not bool(self.settings_manager.get("auto_download_models", True)):
            answer = QMessageBox.question(
                self,
                "Download Whisper model?",
                "The selected Whisper model is missing. Download it now so voice typing can work?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self.model_manager.is_large_model() and bool(self.settings_manager.get("warn_before_large_models", True)):
            if not self._confirm_model_choice(selected):
                return

        self._is_preparing_model = True
        self.model_progress.setVisible(True)
        self.model_progress.setRange(0, 0)
        self.model_status.setText(f"Preparing {selected} model. This can take a while the first time.")
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
        self._update_model_buttons(model_name)
        message = self.transcriber.runtime_message
        if message:
            self.model_status.setText(f"Model ready: {model_name}. {message}")
            self.set_status("Ready", message)
        else:
            self.model_status.setText(f"Model ready: {model_name}")
            shortcut = str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q"))
            self.set_status("Ready", f"Click anywhere, {self._mode_label().lower()} {shortcut}, speak, then release.")

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

    def _append_activity_log(self, message: str) -> None:
        log_path = self.settings_manager.config_dir / "activity.log"
        try:
            with log_path.open("a", encoding="utf-8") as file:
                file.write(f"{message}\n")
        except OSError:
            pass

    def start_listening(self) -> None:
        if self._is_listening or self._is_transcribing:
            self._append_activity_log("start_listening ignored: already listening or transcribing")
            return
        if self._is_preparing_model:
            self._append_activity_log("start_listening blocked: model still preparing")
            self.set_status("Preparing", "The Whisper model is still being prepared. Try again when it says Ready.")
            return
        if not self.model_manager.is_model_ready():
            self._append_activity_log("start_listening triggered model prepare")
            self.prepare_selected_model(auto=True)
            self.set_status("Preparing", "Preparing the selected Whisper model first.")
            return

        microphone_id = self.mic_combo.currentData() or "default"
        current_target = self.text_inserter.get_foreground_window()
        own_window_hwnd = int(self.winId())
        if current_target and current_target.window_hwnd != own_window_hwnd:
            self._target_hwnd = current_target
        else:
            self._target_hwnd = None
        self._append_activity_log(f"start_listening target={self._target_hwnd} microphone={microphone_id}")
        try:
            self.audio_recorder.start(str(microphone_id), self.level_changed.emit)
        except AudioRecorderError as exc:
            self._append_activity_log(f"audio_recorder.start failed: {exc}")
            self.set_status("Error", str(exc))
            return

        self._is_listening = True
        self.mic_button.setText("STOP")
        self.set_status("Listening", "Speak clearly. Release the shortcut to transcribe and type.")

    def stop_listening_and_transcribe(self, release_window_hwnd: object = None) -> None:
        if not self._is_listening:
            self._append_activity_log("stop_listening ignored: not listening")
            return
        self._is_listening = False
        self.mic_button.setText("MIC")
        self.level_bar.setValue(0)
        try:
            release_window = int(release_window_hwnd) if release_window_hwnd is not None else None
        except (TypeError, ValueError):
            release_window = None
        if release_window:
            captured_target = self.text_inserter.get_window_target(release_window)
            if captured_target is not None:
                self._target_hwnd = captured_target
        self._append_activity_log(f"stop_listening release_window={release_window} target={self._target_hwnd}")

        try:
            audio_path = self.audio_recorder.stop()
        except AudioRecorderError as exc:
            self._append_activity_log(f"audio_recorder.stop failed: {exc}")
            self.set_status("Error", str(exc))
            return

        self._is_transcribing = True
        self._transcription_request_id += 1
        request_id = self._transcription_request_id
        self._active_transcription_id = request_id
        self._append_activity_log(f"audio captured: {audio_path}")
        self.set_status("Transcribing", "Local Whisper is turning your speech into text.")
        settings = self.settings_manager.all()
        self._start_transcription_timer(request_id, audio_path)
        threading.Thread(target=self._transcribe_worker, args=(request_id, audio_path, settings), daemon=True).start()

    def cancel_listening(self) -> None:
        if not self._is_listening:
            return
        self._is_listening = False
        self.mic_button.setText("MIC")
        self.audio_recorder.cancel()
        self._append_activity_log("recording cancelled")
        self.level_bar.setValue(0)
        self.set_status("Ready", "Recording cancelled.")

    def _transcribe_worker(self, request_id: int, audio_path: str, settings: dict[str, Any]) -> None:
        self._append_activity_log(f"transcribe_worker start: id={request_id} path={audio_path}")
        try:
            text = self.transcriber.transcribe(audio_path, settings)
        except Exception as exc:
            self._append_activity_log(f"transcribe_worker failed: id={request_id} error={exc}")
            self.transcription_failed.emit(request_id, str(exc), audio_path)
        else:
            self._append_activity_log(f"transcribe_worker success: id={request_id} text={text!r}")
            self.transcription_done.emit(request_id, text, audio_path)

    def finish_transcription(self, request_id: int, text: str, audio_path: str) -> None:
        if request_id != self._active_transcription_id:
            self._append_activity_log(f"finish_transcription ignored stale id={request_id}")
            self._delete_temp_audio(audio_path)
            return
        self._cancel_transcription_timer()
        self._active_transcription_id = 0
        self._is_transcribing = False
        self._delete_temp_audio(audio_path)
        self._append_activity_log(f"finish_transcription id={request_id} text={text!r} target={self._target_hwnd}")
        self.last_transcript.setPlainText(text)
        if not text:
            self._append_activity_log("finish_transcription no speech detected")
            self.set_status("Ready", "No speech was detected. Try speaking closer to the microphone.")
            self._target_hwnd = None
            return

        self.set_status("Typing", "Typing into the focused Windows app.")
        try:
            self.text_inserter.insert_text(text, self._target_hwnd, self.settings_manager.all())
        except TextInsertionError as exc:
            self._append_activity_log(f"text insertion failed: {exc}")
            self.set_status("Error", str(exc))
            self._target_hwnd = None
            return
        self._append_activity_log("text insertion completed")
        detail = self.transcriber.runtime_message or "Done. Click anywhere and use the shortcut again."
        self.set_status("Ready", detail)
        self._target_hwnd = None

    def fail_transcription(self, request_id: int, message: str, audio_path: str) -> None:
        if request_id != self._active_transcription_id:
            self._append_activity_log(f"fail_transcription ignored stale id={request_id}: {message}")
            self._delete_temp_audio(audio_path)
            return
        self._cancel_transcription_timer()
        self._active_transcription_id = 0
        self._is_transcribing = False
        self._delete_temp_audio(audio_path)
        self._append_activity_log(f"fail_transcription id={request_id}: {message}")
        self.set_status("Error", message)
        self._target_hwnd = None

    def handle_transcription_timeout(self, request_id: int, audio_path: str) -> None:
        if request_id != self._active_transcription_id or not self._is_transcribing:
            return
        self._cancel_transcription_timer()
        self._active_transcription_id = 0
        self._is_transcribing = False
        self._target_hwnd = None
        self._append_activity_log(f"transcription timeout id={request_id} path={audio_path}")
        self._reset_transcriber()
        self.set_status(
            "Error",
            "Transcription took too long, so Whisper Anywhere reset the speech engine. Try again or switch to CPU / a smaller model.",
        )

    def _delete_temp_audio(self, audio_path: str) -> None:
        if not bool(self.settings_manager.get("delete_temp_audio", True)):
            return
        try:
            Path(audio_path).unlink(missing_ok=True)
        except OSError:
            pass

    def apply_shortcut_settings(self) -> None:
        shortcut = self.shortcut_input.text().strip() or "Ctrl+Alt+Q"
        try:
            parse_shortcut(shortcut)
        except ValueError as exc:
            QMessageBox.warning(self, "Shortcut problem", str(exc))
            return

        warning = shortcut_warning(shortcut)
        if warning:
            QMessageBox.warning(self, "Shortcut warning", warning)
        mode = str(self.mode_combo.currentData() or "hold")
        self.shortcut_changed.emit(shortcut, mode)
        self.settings_manager.update({"shortcut": shortcut, "shortcut_mode": mode})
        self.shortcut_reminder.setText(f"{self._mode_label()} {shortcut} to talk")
        self.update_bottom_labels()
        self.set_status("Ready", f"Shortcut saved: {shortcut}")
        self.set_hotkey_status(f"Shortcut saved: {shortcut}. Watch the hotkey log while testing.")

    def apply_performance_preset(self) -> None:
        if self._loading_ui:
            return
        if self._pipeline_busy():
            self._restore_performance_controls()
            self.set_status("Busy", self._pipeline_busy_message())
            return
        preset = str(self.performance_combo.currentData() or "balanced")
        model_hint = None
        self._loading_ui = True
        try:
            if preset == "fast":
                model_hint = "base"
                self._select_combo_data(self.device_combo, "cpu")
                self._select_combo_data(self.compute_combo, "int8")
                self.low_ram_checkbox.setChecked(False)
                self.low_vram_checkbox.setChecked(False)
                self.use_gpu_checkbox.setChecked(False)
            elif preset == "balanced":
                model_hint = "base"
                self._select_combo_data(self.device_combo, "cpu")
                self._select_combo_data(self.compute_combo, "int8")
                self.low_ram_checkbox.setChecked(False)
                self.low_vram_checkbox.setChecked(False)
                self.use_gpu_checkbox.setChecked(False)
            elif preset == "accurate":
                model_hint = "medium"
                self._select_combo_data(self.device_combo, "cpu")
                self._select_combo_data(self.compute_combo, "int8")
                self.low_ram_checkbox.setChecked(False)
                self.low_vram_checkbox.setChecked(False)
                self.use_gpu_checkbox.setChecked(False)
            elif preset == "low_ram":
                model_hint = "base"
                self._select_combo_data(self.device_combo, "cpu")
                self._select_combo_data(self.compute_combo, "int8")
                self.low_ram_checkbox.setChecked(True)
                self.low_vram_checkbox.setChecked(False)
                self.use_gpu_checkbox.setChecked(False)
            elif preset == "low_vram":
                model_hint = "small"
                self._select_combo_data(self.device_combo, "cpu")
                self._select_combo_data(self.compute_combo, "int8")
                self.low_ram_checkbox.setChecked(False)
                self.low_vram_checkbox.setChecked(True)
                self.use_gpu_checkbox.setChecked(False)
        finally:
            self._loading_ui = False

        self.save_performance_settings(prepare=False)
        if model_hint and model_hint != self.model_manager.selected_model():
            self.select_model(model_hint)
        else:
            self.prepare_selected_model(auto=True)
        if self._is_preparing_model:
            self.set_status("Preparing", f"Applying {self.performance_combo.currentText()} mode and preparing the model.")
        else:
            self.set_status("Ready", f"Performance mode saved: {self.performance_combo.currentText()}")

    def save_microphone_setting(self) -> None:
        if self._loading_ui:
            return
        value = self.mic_combo.currentData() or "default"
        self.settings_manager.set("microphone_device", str(value))
        self.update_bottom_labels()

    def save_typing_settings(self) -> None:
        if self._loading_ui:
            return
        self.settings_manager.update(
            {
                "insert_method": str(self.insert_method_combo.currentData() or "clipboard_paste"),
                "restore_clipboard": self.restore_clipboard_checkbox.isChecked(),
            }
        )

    def save_performance_settings(self, prepare: bool = True) -> None:
        if self._loading_ui:
            return
        if self._pipeline_busy():
            self._restore_performance_controls()
            self.set_status("Busy", self._pipeline_busy_message())
            return
        self.settings_manager.update(
            {
                "performance_preset": str(self.performance_combo.currentData() or "balanced"),
                "device": str(self.device_combo.currentData() or "auto"),
                "compute_type": str(self.compute_combo.currentData() or "auto"),
                "cpu_threads": self.cpu_threads_combo.currentData() or "auto",
                "low_ram_mode": self.low_ram_checkbox.isChecked(),
                "low_vram_mode": self.low_vram_checkbox.isChecked(),
                "fallback_to_cpu": self.fallback_cpu_checkbox.isChecked(),
                "warn_before_large_models": self.warn_large_checkbox.isChecked(),
                "auto_download_models": self.auto_download_checkbox.isChecked(),
                "use_gpu_if_available": self.use_gpu_checkbox.isChecked(),
            }
        )
        self._reset_transcriber()
        self.update_bottom_labels()
        if prepare and self.auto_download_checkbox.isChecked():
            self.prepare_selected_model(auto=True)

    def update_shortcut_warning(self) -> None:
        warning = shortcut_warning(self.shortcut_input.text().strip() or "Ctrl+Alt+Q")
        self.shortcut_warning.setText(warning)

    def update_level(self, level: float) -> None:
        self.level_bar.setValue(int(level * 100))

    def set_status(self, status: str, detail: str) -> None:
        self.status_label.setText(status)
        self.status_detail.setText(detail)
        self.bottom_status.setText(status)

    def set_hotkey_status(self, message: str) -> None:
        self.hotkey_status_label.setText(message)

    def show_hotkey_error(self, message: str) -> None:
        self.set_hotkey_status(message)
        self.set_status("Hotkey Error", message)
        QMessageBox.warning(self, "Hotkey error", message)

    def update_bottom_labels(self) -> None:
        model = self.model_manager.selected_model()
        mic_name = self.mic_combo.currentText() or "Default system microphone"
        shortcut = str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q"))
        device = str(self.settings_manager.get("device", "auto"))
        self.bottom_model.setText(f"Whisper: {model} ({device})")
        self.bottom_mic.setText(f"Mic: {mic_name}")
        self.bottom_shortcut.setText(f"Shortcut: {shortcut}")
        self.shortcut_reminder.setText(f"{self._mode_label()} {shortcut} to talk")

    def _mode_label(self) -> str:
        return "Press" if str(self.settings_manager.get("shortcut_mode", "hold")) == "toggle" else "Hold"

    def _select_combo_data(self, combo: QComboBox, value: Any) -> None:
        was_blocked = combo.blockSignals(True)
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(was_blocked)

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
            #modelCard { min-height: 148px; text-align: left; font-weight: 700; }
            QLineEdit, QComboBox, QTextEdit { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 8px; color: #f8fafc; }
            QProgressBar { background: #111827; border: 1px solid #334155; border-radius: 7px; }
            QProgressBar::chunk { background: #8b5cf6; border-radius: 7px; }
            QCheckBox { spacing: 8px; }
            QGroupBox { font-weight: 800; margin-top: 12px; padding: 14px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
            """
        )
