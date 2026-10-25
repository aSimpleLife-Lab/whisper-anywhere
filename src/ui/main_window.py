from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from core import app_log, voz_engine
from core.app_icon import app_icon, apply_native_window_icon
from core.app_version import APP_VERSION, GITHUB_RELEASES_API, GITHUB_RELEASES_URL
from core.audio_recorder import SAMPLE_RATE, AudioRecorder, AudioRecorderError
from core.hotkey_listener import parse_shortcut, shortcut_warning
from core.model_manager import LARGE_MODELS, MODEL_INFO, ModelManager
from core.settings_manager import SettingsManager
from core.startup_manager import StartupManager, StartupManagerError
from core.text_inserter import TextInserter, TextInsertionError, TextTarget
from core.transcriber import Transcriber
from ui.horizon import MicWheel, VoiceHorizon
from ui.widgets import (
    COLORS,
    GLYPHS,
    SHADOW,
    SHELL_RADIUS,
    CubeTitle,
    Meter,
    SafeComboBox,
    Segmented,
    ShellDialog,
    ShortcutCaptureInput,
    ToggleRow,
    button,
    divider,
    label,
    paint_shell,
    show_message,
    stylesheet,
)

TRANSCRIPTION_TIMEOUT_SECONDS = 90.0
MODEL_PREPARE_TIMEOUT_SECONDS = 900.0
WARM_UP_DELAY_MS = 1500
ERROR_STATUSES = {"Error", "Hotkey error", "Startup setting problem", "Update check failed"}
SPEED_LEVELS = {"Fastest": 5, "Very fast": 5, "Fast": 4, "Balanced": 3, "Moderate": 2, "Slow": 1}
ACCURACY_LEVELS = {
    "Highest": 5,
    "Higher": 4,
    "High": 4,
    "Very good": 3,
    "Good": 3,
    "Basic": 2,
    "Lowest": 1,
}
ENGINE_LABELS = {"voz": "Parakeet TDT", "whisper": "Whisper", "funasr": "FunASR"}
RESIZE_GRIP = SHADOW + 4
MODEL_ROW = 30  # model list row height


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value)
    return tuple(int(part) for part in parts[:4]) if parts else (0,)


def _sanitize_path_text(value: str) -> str:
    return re.sub(r"[A-Za-z]:\\Users\\[^\\\r\n]+", r"%USERPROFILE%", str(value or ""))


def _folder_bytes(folder: Path) -> int:
    total = 0
    try:
        for path in folder.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except OSError:
        pass
    return total


def _logo(size: int) -> QLabel:
    logo = QLabel()
    # QIcon picks the frame for this screen's scaling; rescaling it would undo that.
    pixmap = app_icon().pixmap(size, size)
    if not pixmap.isNull():
        logo.setPixmap(pixmap)
    logo.setFixedSize(size, size)
    return logo


def _panel() -> QFrame:
    frame = QFrame()
    frame.setObjectName("panel")
    return frame


class _ModelDelegate(QStyledItemDelegate):
    """Name on the left, install state on the right, like LumaDeck's display list."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, MODEL_ROW)

    def paint(self, painter: QPainter, option, index) -> None:
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        text = item.text
        item.text = ""
        if item.widget is not None:
            # Keep the row inside the viewport so the state stays visible.
            item.rect.setRight(min(item.rect.right(), item.widget.viewport().rect().right() - 1))
        style = item.widget.style() if item.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, item, painter, item.widget)
        name = str(index.data(Qt.ItemDataRole.UserRole))
        manager = self.window.model_manager
        if name == manager.selected_model():
            state, colour = "In use", COLORS["cyan"]
        elif manager.is_model_ready(name):
            state, colour = "Installed", COLORS["muted"]
        else:
            state, colour = MODEL_INFO[name].approximate_size, "#6B7782"
        rect = item.rect.adjusted(10, 0, -10, 0)
        painter.save()
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        font = painter.font()
        font.setPixelSize(11)
        font.setBold(state == "In use")
        painter.setFont(font)
        painter.setPen(QColor(colour))
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, state)
        painter.restore()


class _Shell(QWidget):
    """Paints LumaDeck's rounded glass shell and resizes from its edges."""

    def __init__(self, window: QMainWindow) -> None:
        super().__init__(window)
        self._window = window
        self.margin = SHADOW
        self.setMouseTracking(True)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        radius = SHELL_RADIUS if self.margin else 0
        paint_shell(painter, QRectF(self.rect()), self.margin, radius)

    def _edges(self, pos: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if not self.margin:
            return edges
        if pos.x() < RESIZE_GRIP:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() > self.width() - RESIZE_GRIP:
            edges |= Qt.Edge.RightEdge
        if pos.y() < RESIZE_GRIP:
            edges |= Qt.Edge.TopEdge
        elif pos.y() > self.height() - RESIZE_GRIP:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mouseMoveEvent(self, event) -> None:
        edges = self._edges(event.position().toPoint())
        cursors = {
            Qt.Edge.LeftEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.RightEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.RightEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeBDiagCursor,
            Qt.Edge.LeftEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeBDiagCursor,
            Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
            Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
        }
        self.setCursor(cursors.get(edges, Qt.CursorShape.ArrowCursor))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        edges = self._edges(event.position().toPoint())
        handle = self._window.windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and edges and handle is not None:
            handle.startSystemResize(edges)
            return
        super().mousePressEvent(event)


class _TitleBar(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window
        self.setFixedHeight(42)

    def mousePressEvent(self, event) -> None:
        handle = self._window.windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and handle is not None:
            handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self._window.toggle_maximized()
        super().mouseDoubleClickEvent(event)


class FirstRunSetupDialog(ShellDialog):
    def __init__(self, parent: "MainWindow") -> None:
        super().__init__(
            parent,
            "Welcome to Whisper Anywhere",
            "Pick a shortcut, a speech model and a microphone. You can change all of it later.",
            width=500,
        )
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.shortcut_input = ShortcutCaptureInput()
        self.shortcut_input.setText(str(parent.settings_manager.get("shortcut", "Ctrl+Alt+Q")))
        form.addRow("Shortcut", self.shortcut_input)
        self.model_combo = SafeComboBox()
        for info in parent.model_manager.models():
            self.model_combo.addItem(f"{info.label}  ·  {info.recommended_use}", info.name)
        parent._select_combo_data(self.model_combo, parent.model_manager.selected_model())
        form.addRow("Speech model", self.model_combo)
        self.mic_combo = SafeComboBox()
        for device in parent.audio_recorder.list_input_devices():
            self.mic_combo.addItem(device.name, device.id)
        parent._select_combo_data(
            self.mic_combo, parent.settings_manager.get("microphone_device", "default")
        )
        form.addRow("Microphone", self.mic_combo)
        self.device_combo = SafeComboBox()
        self.device_combo.addItem("CPU (recommended)", "cpu")
        self.device_combo.addItem("Auto: GPU when available", "auto")
        self.device_combo.addItem("GPU preferred", "gpu")
        parent._select_combo_data(self.device_combo, parent.settings_manager.get("device", "cpu"))
        form.addRow("Hardware", self.device_combo)
        self.body.addLayout(form)
        self.body.addWidget(
            label(
                f"Voz is recommended: the fastest and most accurate model here for 25 European "
                f"languages, and it needs no GPU. It downloads once ({voz_engine.DOWNLOAD_MB} MB).",
                "small",
                wrap=True,
            )
        )
        row = QHBoxLayout()
        row.addStretch(1)
        skip = button("Skip for now")
        skip.clicked.connect(self.reject)
        save = button("Save and start", "primary")
        save.clicked.connect(self.accept)
        row.addWidget(skip)
        row.addWidget(save)
        self.body.addSpacing(4)
        self.body.addLayout(row)


class SettingsWindow(ShellDialog):
    """The gear in the title bar: system behaviour, privacy and support."""

    def __init__(self, parent: "MainWindow") -> None:
        super().__init__(parent, "Settings", "System behaviour, privacy and support", width=500)
        system = QFrame()
        system.setObjectName("glass")
        system_layout = QVBoxLayout(system)
        system_layout.setContentsMargins(14, 6, 14, 6)
        system_layout.setSpacing(0)
        self.start_with_windows = ToggleRow("Start with Windows", "Launches hidden in the tray when you sign in.")
        self.keep_in_tray = ToggleRow("Keep running in tray when closed", "The shortcut keeps working; exit from the tray menu.")
        self.debug_log = ToggleRow("Write a debug log to disk", "Event names and timings only. Nothing you say is logged.")
        for index, row in enumerate((self.start_with_windows, self.keep_in_tray, self.debug_log)):
            if index:
                system_layout.addWidget(divider())
            system_layout.addWidget(row)
        self.body.addWidget(system)

        # The rarely touched model and GPU switches live here, so the main
        # window's columns fit without scrolling.
        self.body.addWidget(CubeTitle("Models and hardware"))
        hardware = QFrame()
        hardware.setObjectName("glass")
        hardware_layout = QVBoxLayout(hardware)
        hardware_layout.setContentsMargins(14, 6, 14, 6)
        hardware_layout.setSpacing(0)
        for index, row in enumerate(
            (parent.low_vram_row, parent.fallback_cpu_row, parent.auto_download_row, parent.warn_large_row)
        ):
            if index:
                hardware_layout.addWidget(divider())
            hardware_layout.addWidget(row)
        self.body.addWidget(hardware)

        self.body.addWidget(CubeTitle("Support"))
        grid = QGridLayout()
        grid.setSpacing(8)
        self.reset_engine_button = button("Reset speech engine")
        self.reset_engine_button.setToolTip("Unload the speech model and load it again")
        self.check_updates_button = button("Check for updates")
        self.copy_diagnostics_button = button("Copy diagnostics")
        self.save_diagnostics_button = button("Save diagnostics")
        self.export_settings_button = button("Export settings")
        self.import_settings_button = button("Import settings")
        self.run_setup_button = button("Run setup again")
        self.open_settings_folder_button = button("Open settings folder")
        self.open_model_folder_button = button("Open model folder")
        for index, widget in enumerate(
            (
                self.check_updates_button,
                self.run_setup_button,
                self.copy_diagnostics_button,
                self.save_diagnostics_button,
                self.export_settings_button,
                self.import_settings_button,
                self.open_settings_folder_button,
                self.open_model_folder_button,
                self.reset_engine_button,
            )
        ):
            widget.setMinimumHeight(34)  # otherwise the dialog squeezes them and clips the text
            grid.addWidget(widget, index // 2, index % 2)
        self.body.addLayout(grid)

        self.body.addWidget(CubeTitle("Recent activity"))
        self.activity_view = QPlainTextEdit()
        self.activity_view.setReadOnly(True)
        self.activity_view.setFixedHeight(96)  # keeps the whole dialog within a 1080p screen
        self.activity_view.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        self.body.addWidget(self.activity_view)

        about = QHBoxLayout()
        about.addWidget(_logo(36))
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("Whisper Anywhere", "strong"))
        self.version_label = label(f"Version {APP_VERSION}", "small")
        names.addWidget(self.version_label)
        about.addLayout(names, 1)
        self.body.addLayout(about)
        credits = label(
            'Speech recognition by <b>Voz</b>, on-device AI by '
            '<a style="color:#55C7D8" href="https://desertant.com">Desert Ant Labs</a> '
            '(<a style="color:#55C7D8" href="https://license.desertant.com/1.0">Source-Available License 1.0</a>), '
            'derived from <a style="color:#55C7D8" href="https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3">'
            "NVIDIA Parakeet TDT 0.6B v3</a> (CC BY 4.0). Whisper by OpenAI via "
            '<a style="color:#55C7D8" href="https://github.com/SYSTRAN/faster-whisper">faster-whisper</a>; '
            'SenseVoice and Fun-ASR via <a style="color:#55C7D8" href="https://github.com/modelscope/FunASR">FunASR</a>.',
            "small",
            wrap=True,
        )
        credits.setTextFormat(Qt.TextFormat.RichText)
        credits.setOpenExternalLinks(True)
        self.body.addWidget(credits)

    def showEvent(self, event) -> None:
        self.activity_view.setPlainText("\n".join(app_log.recent(120)))
        self.activity_view.verticalScrollBar().setValue(self.activity_view.verticalScrollBar().maximum())
        super().showEvent(event)


class MainWindow(QMainWindow):
    model_changed = Signal(str)
    shortcut_changed = Signal(str, str)
    hotkey_enabled_changed = Signal(bool)
    feedback_volume_changed = Signal(float)
    state_changed = Signal(str)
    level_changed = Signal(float)
    transcription_done = Signal(int, str, float, float)
    transcription_failed = Signal(int, str)
    insertion_done = Signal(str)
    transcription_timeout = Signal(int)
    model_ready = Signal(int, str)
    model_failed = Signal(int, str)
    model_prepare_timeout = Signal(int)
    warm_up_done = Signal(int, str)
    update_checked = Signal(bool, str, str)

    def __init__(
        self,
        settings_manager: SettingsManager,
        model_manager: ModelManager,
        audio_recorder: AudioRecorder,
        transcriber: Transcriber,
        text_inserter: TextInserter,
        startup_manager: StartupManager,
    ) -> None:
        super().__init__()
        self.settings_manager = settings_manager
        self.model_manager = model_manager
        self.audio_recorder = audio_recorder
        self.transcriber = transcriber
        self.text_inserter = text_inserter
        self.startup_manager = startup_manager
        self._force_exit = False
        self._target_hwnd: TextTarget | None = None
        self._is_listening = False
        self._is_transcribing = False
        self._is_preparing_model = False
        self._is_capturing_shortcut = False
        self._loading_ui = False
        self._status_is_error = False
        self._last_state = ""
        self._transcription_request_id = 0
        self._active_transcription_id = 0
        self._transcription_timer: threading.Timer | None = None
        self._model_prepare_request_id = 0
        self._active_model_prepare_id = 0
        self._model_prepare_timer: threading.Timer | None = None
        self._model_prepare_started_at = 0.0
        self._warm_up_generation = 0
        self._model_progress_timer = QTimer(self)
        self._model_progress_timer.setInterval(1000)
        self._model_progress_timer.timeout.connect(self._tick_model_progress)
        self._warm_up_timer = QTimer(self)
        self._warm_up_timer.setSingleShot(True)
        self._warm_up_timer.setInterval(WARM_UP_DELAY_MS)
        self._warm_up_timer.timeout.connect(self.warm_up_selected_model)

        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Whisper Anywhere")
        self.setWindowIcon(app_icon())
        # Small minimum: on short or narrow screens the body scrolls instead of being cut off.
        self.setMinimumSize(640 + 2 * SHADOW, 420 + 2 * SHADOW)
        self.setStyleSheet(stylesheet())
        self._build_ui()
        self._fit_to_screen()
        self.settings_window = SettingsWindow(self)
        apply_native_window_icon(int(self.winId()))
        self._connect_signals()
        self._load_settings_into_ui()
        self.set_status("Ready", self._ready_detail())
        self._refresh_model_status()
        if not bool(self.settings_manager.get("first_run_setup_completed", False)):
            QTimer.singleShot(500, self.show_first_run_setup)
        self._warm_up_timer.start()

    # ------------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        self.shell = _Shell(self)
        self.shell_layout = QVBoxLayout(self.shell)
        self.shell_layout.setSpacing(0)
        self.shell_layout.addWidget(self._build_title_bar())
        body = QWidget()
        body.setObjectName("scrollContent")
        columns = QHBoxLayout(body)
        columns.setContentsMargins(14, 0, 14, 14)
        columns.setSpacing(16)
        columns.addWidget(self._build_models_column())
        columns.addWidget(self._build_center_column(), 1)
        columns.addWidget(self._build_settings_column())
        # Where the screen is big enough this changes nothing; on smaller screens the
        # columns scroll instead of squeezing their rows and clipping text.
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.viewport().setAutoFillBackground(False)
        self.shell_layout.addWidget(scroll, 1)
        self.setCentralWidget(self.shell)
        self._apply_frame_margins()

    def _fit_to_screen(self) -> None:
        """Open at the designed size where it fits, otherwise as large as the screen allows, centred."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1220 + 2 * SHADOW, 780 + 2 * SHADOW)
            return
        area = screen.availableGeometry()
        width = min(1220 + 2 * SHADOW, area.width())
        height = min(780 + 2 * SHADOW, area.height())
        self.resize(width, height)
        self.move(area.x() + (area.width() - width) // 2, area.y() + (area.height() - height) // 2)

    def _build_title_bar(self) -> QWidget:
        bar = _TitleBar(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(_logo(20))
        layout.addWidget(label("Whisper Anywhere", "windowTitle"))
        layout.addStretch(1)
        self.settings_button = button(GLYPHS["settings"], "caption")
        self.settings_button.setToolTip("Settings")
        self.minimize_button = button(GLYPHS["minimize"], "caption")
        self.maximize_button = button(GLYPHS["maximize"], "caption")
        self.close_button = button(GLYPHS["close"], "captionClose")
        for widget in (self.settings_button, self.minimize_button, self.maximize_button, self.close_button):
            widget.setCursor(Qt.CursorShape.ArrowCursor)
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            layout.addWidget(widget)
        return bar

    def _build_models_column(self) -> QWidget:
        panel = _panel()
        panel.setFixedWidth(260)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(0)
        layout.addWidget(CubeTitle("Speech models"))
        layout.addSpacing(8)
        self.model_list = QListWidget()
        self.model_list.setItemDelegate(_ModelDelegate(self))
        self.model_list.setUniformItemSizes(True)
        self.model_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.model_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for info in self.model_manager.models():
            item = QListWidgetItem(info.label)
            item.setData(Qt.ItemDataRole.UserRole, info.name)
            self.model_list.addItem(item)
        # Every model is visible at once, so nothing in this column scrolls.
        self.model_list.setFixedHeight(self.model_list.count() * (MODEL_ROW + 2) + 6)
        layout.addWidget(self.model_list)
        layout.addSpacing(14)

        details = QFrame()
        details.setObjectName("inset")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(12, 10, 12, 12)
        details_layout.setSpacing(6)
        self.detail_title = label("", "strong")
        self.detail_languages = label("", "small", wrap=True)
        self.detail_use = label("", "muted", wrap=True)
        details_layout.addWidget(self.detail_title)
        details_layout.addWidget(self.detail_languages)
        details_layout.addWidget(self.detail_use)
        stats = QGridLayout()
        stats.setHorizontalSpacing(10)
        stats.setVerticalSpacing(6)
        self.speed_meter = Meter()
        self.accuracy_meter = Meter()
        for row, (caption, widget) in enumerate(
            (("Speed", self.speed_meter), ("Accuracy", self.accuracy_meter))
        ):
            stats.addWidget(label(caption, "small"), row, 0)
            stats.addWidget(widget, row, 1, Qt.AlignmentFlag.AlignVCenter)
        stats.setColumnStretch(1, 1)
        details_layout.addLayout(stats)
        self.detail_needs = label("", "small", wrap=True)
        details_layout.addWidget(self.detail_needs)
        layout.addWidget(details)
        layout.addSpacing(10)
        self.use_model_button = button("Use this model", "primary")
        layout.addWidget(self.use_model_button)
        layout.addSpacing(8)
        self.model_progress = QProgressBar()
        self.model_progress.setTextVisible(False)
        self.model_progress.setVisible(False)
        layout.addWidget(self.model_progress)
        self.model_status = label("", "small", wrap=True)
        layout.addWidget(self.model_status)
        layout.addStretch(1)
        return panel

    def _build_center_column(self) -> QWidget:
        panel = _panel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.addStretch(1)
        master = QVBoxLayout()
        master.setSpacing(0)
        self.dictation_toggle = ToggleRow("Dictation on")
        self.dictation_toggle.setToolTip("Pause or resume the global shortcut")
        master.addWidget(self.dictation_toggle)
        header.addLayout(master)
        layout.addLayout(header)

        scene = QHBoxLayout()
        scene.setContentsMargins(0, 16, 0, 14)
        scene.setSpacing(16)
        self.horizon = VoiceHorizon()
        self.horizon.setFixedHeight(216)
        scene.addWidget(self.horizon, 1)
        self.mic_button = MicWheel()
        self.mic_button.setToolTip("Click to start or stop listening")
        scene.addWidget(self.mic_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(scene)

        self.status_detail = label("", "muted", wrap=True)
        layout.addWidget(self.status_detail)
        layout.addSpacing(14)

        rows = QGridLayout()
        rows.setHorizontalSpacing(12)
        rows.setVerticalSpacing(12)
        rows.setColumnMinimumWidth(0, 104)
        rows.addWidget(label("Microphone"), 0, 0)
        self.mic_combo = SafeComboBox()
        rows.addWidget(self.mic_combo, 0, 1)
        self.refresh_mics_button = button(GLYPHS["refresh"], "icon")
        self.refresh_mics_button.setToolTip("Look for microphones again")
        rows.addWidget(self.refresh_mics_button, 0, 2)
        rows.addWidget(label("Shortcut"), 1, 0)
        shortcut = QHBoxLayout()
        shortcut.setSpacing(10)
        self.shortcut_input = ShortcutCaptureInput()
        self.shortcut_input.setMinimumWidth(120)  # narrow windows scroll sideways rather than crush it
        shortcut.addWidget(self.shortcut_input, 1)
        self.mode_segmented = Segmented([("Hold to talk", "hold"), ("Toggle", "toggle")])
        self.mode_segmented.setFixedWidth(184)
        shortcut.addWidget(self.mode_segmented)
        rows.addLayout(shortcut, 1, 1, 1, 2)
        rows.addWidget(label("Sound effects"), 2, 0)
        volume = QHBoxLayout()
        volume.setSpacing(10)
        self.feedback_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.feedback_volume_slider.setRange(0, 100)
        self.feedback_volume_slider.setPageStep(10)
        self.feedback_volume_slider.setAccessibleName("Sound effects volume")
        self.feedback_volume_slider.setToolTip("Volume of the start and stop sounds. 0 turns them off.")
        volume.addWidget(self.feedback_volume_slider, 1)
        self.feedback_volume_value = label("50%", "muted")
        self.feedback_volume_value.setMinimumWidth(36)
        self.feedback_volume_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        volume.addWidget(self.feedback_volume_value)
        rows.addLayout(volume, 2, 1, 1, 2)
        # Save and preview once the slider rests, not on every step of a drag.
        self._feedback_volume_timer = QTimer(self)
        self._feedback_volume_timer.setSingleShot(True)
        self._feedback_volume_timer.setInterval(250)
        self._feedback_volume_timer.timeout.connect(self.save_feedback_volume)
        rows.setColumnStretch(1, 1)
        layout.addLayout(rows)
        self.shortcut_warning = label("", "warning", wrap=True)
        layout.addWidget(self.shortcut_warning)
        self.hotkey_status_label = label("Starting the global shortcut listener.", "small", wrap=True)
        layout.addSpacing(6)
        layout.addWidget(self.hotkey_status_label)
        layout.addSpacing(14)

        transcript_header = QHBoxLayout()
        transcript_header.addWidget(CubeTitle("Last transcription"))
        transcript_header.addStretch(1)
        transcript_header.addWidget(label("Kept in memory only", "small"))
        layout.addLayout(transcript_header)
        layout.addSpacing(6)
        self.last_transcript = QPlainTextEdit()
        self.last_transcript.setReadOnly(True)
        # Takes whatever height is left; Qt's ~190 px default hint would make short screens scroll.
        self.last_transcript.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.last_transcript.setMinimumHeight(84)
        self.last_transcript.setPlaceholderText(
            "Click into any app, hold the shortcut, speak, then let go. The text is typed where your cursor is."
        )
        layout.addWidget(self.last_transcript, 1)
        layout.addSpacing(14)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.listen_button = button("Start listening", "primary")
        actions.addWidget(self.listen_button, 1)
        self.copy_transcript_button = button("Copy")
        self.clear_transcript_button = button("Clear")
        actions.addWidget(self.copy_transcript_button)
        actions.addWidget(self.clear_transcript_button)
        layout.addLayout(actions)
        return panel

    def _build_settings_column(self) -> QWidget:
        column = QWidget()
        column.setFixedWidth(318)
        stack = QVBoxLayout(column)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(14)
        panel = _panel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(0)

        layout.addWidget(CubeTitle("Typing", "columnTitle"))
        layout.addWidget(label("How the text arrives in your apps.", "muted", wrap=True))
        layout.addSpacing(8)
        self.restore_clipboard_row = ToggleRow("Restore the clipboard")
        self.add_space_row = ToggleRow("Add a space after the text")
        self.press_enter_row = ToggleRow("Press Enter after the text", "Sends the message in chat apps.")
        self.capitalize_row = ToggleRow("Capitalize the first letter")
        self.period_row = ToggleRow("End with a full stop")
        for row in (
            self.restore_clipboard_row,
            self.add_space_row,
            self.press_enter_row,
            self.capitalize_row,
            self.period_row,
        ):
            layout.addWidget(row)

        layout.addSpacing(14)
        layout.addWidget(CubeTitle("Performance", "columnTitle"))
        layout.addWidget(
            label("Mostly for Whisper and FunASR.", "muted", wrap=True)
        )
        layout.addSpacing(10)
        self.device_segmented = Segmented([("Auto", "auto"), ("CPU", "cpu"), ("GPU", "gpu")])
        self.device_segmented.setToolTip("Where Whisper and FunASR run")
        layout.addWidget(self.device_segmented)
        layout.addSpacing(12)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.cpu_threads_combo = SafeComboBox()
        self.cpu_threads_combo.addItem("Auto", "auto")
        for value in (2, 4, 6, 8, 12, 16, 24, 32):
            if value <= (os.cpu_count() or 8):
                self.cpu_threads_combo.addItem(f"{value} threads", value)
        self.cpu_threads_combo.setToolTip(
            "Auto uses one thread per physical core; more than that slows speech models down."
        )
        self.compute_combo = SafeComboBox()
        for text, value in (
            ("Auto", "auto"),
            ("int8", "int8"),
            ("int8 + float16", "int8_float16"),
            ("float16", "float16"),
            ("float32", "float32"),
        ):
            self.compute_combo.addItem(text, value)
        grid.addWidget(label("CPU threads"), 0, 0)
        grid.addWidget(self.cpu_threads_combo, 0, 1)
        grid.addWidget(label("Whisper precision"), 1, 0)
        grid.addWidget(self.compute_combo, 1, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        layout.addSpacing(6)
        self.keep_model_loaded_row = ToggleRow("Keep the model loaded", "Dictation starts instantly.")
        self.low_ram_row = ToggleRow("Low RAM mode", "Unload after every dictation, CPU only.")
        layout.addWidget(self.keep_model_loaded_row)
        layout.addWidget(self.low_ram_row)
        layout.addStretch(1)
        stack.addWidget(panel, 1)
        # These four live in the Settings window (see SettingsWindow).
        self.low_vram_row = ToggleRow("Low VRAM mode", "int8 on the GPU, next to games.")
        self.fallback_cpu_row = ToggleRow("Fall back to the CPU", "If the GPU fails, keep going.")
        self.auto_download_row = ToggleRow("Download models automatically")
        self.warn_large_row = ToggleRow("Warn before large models")

        # Pinned under the settings, like LumaDeck's weather card.
        card = QFrame()
        card.setObjectName("glass")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(3)
        top = QHBoxLayout()
        top.addWidget(CubeTitle("Last dictation", "cardTitle"))
        top.addStretch(1)
        self.last_value = label("--", "amberValue")
        top.addWidget(self.last_value)
        card_layout.addLayout(top)
        self.last_condition = label("No dictation yet", "strong")
        self.last_detail = label("Speak and the timing shows up here.", "small", wrap=True)
        self.last_source = label("Nothing is saved to disk", "small")
        card_layout.addWidget(self.last_condition)
        card_layout.addWidget(self.last_detail)
        card_layout.addWidget(self.last_source)
        stack.addWidget(card)
        return column

    def _apply_frame_margins(self) -> None:
        margin = 0 if self.isMaximized() else SHADOW
        self.shell.margin = margin
        self.shell_layout.setContentsMargins(margin, margin, margin, margin)
        self.maximize_button.setText(GLYPHS["restore"] if self.isMaximized() else GLYPHS["maximize"])
        self.shell.update()

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.WindowStateChange:
            self._apply_frame_margins()
            self.horizon.set_animated(self.isVisible() and not self.isMinimized())
        elif event.type() == event.Type.ActivationChange:
            # Clouds drift briskly only while the window has focus.
            self.horizon.set_window_active(self.isActiveWindow())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.horizon.set_animated(True)

    def hideEvent(self, event) -> None:
        self.horizon.set_animated(False)
        super().hideEvent(event)

    # ----------------------------------------------------------------- signals

    def _connect_signals(self) -> None:
        self.settings_button.clicked.connect(lambda checked=False: self.open_settings_window())
        self.minimize_button.clicked.connect(lambda checked=False: self.showMinimized())
        self.maximize_button.clicked.connect(lambda checked=False: self.toggle_maximized())
        self.close_button.clicked.connect(lambda checked=False: self.close())
        self.mic_button.clicked.connect(lambda checked=False: self.toggle_manual_listening())
        self.listen_button.clicked.connect(lambda checked=False: self.toggle_manual_listening())
        self.copy_transcript_button.clicked.connect(lambda checked=False: self.copy_transcript())
        self.clear_transcript_button.clicked.connect(lambda checked=False: self.last_transcript.clear())
        self.model_list.currentItemChanged.connect(lambda current, previous: self._show_model_details())
        self.model_list.itemDoubleClicked.connect(lambda item: self.use_highlighted_model())
        self.use_model_button.clicked.connect(lambda checked=False: self.use_highlighted_model())
        self.settings_window.reset_engine_button.clicked.connect(
            lambda checked=False: self.reset_speech_engine()
        )
        self.dictation_toggle.switch.toggled.connect(self._dictation_toggled)
        self.shortcut_input.textChanged.connect(lambda text="": self.update_shortcut_warning())
        self.shortcut_input.capture_started.connect(self.start_shortcut_capture)
        self.shortcut_input.shortcut_captured.connect(self.finish_shortcut_capture)
        self.shortcut_input.capture_cancelled.connect(self.cancel_shortcut_capture)
        self.mode_segmented.changed.connect(lambda value: self.apply_shortcut_settings())
        self.mic_combo.currentIndexChanged.connect(lambda index=0: self.save_microphone_setting())
        self.refresh_mics_button.clicked.connect(lambda checked=False: self.refresh_microphones())
        for row in (
            self.restore_clipboard_row,
            self.add_space_row,
            self.press_enter_row,
            self.capitalize_row,
            self.period_row,
        ):
            row.switch.toggled.connect(lambda checked=False: self.save_typing_settings())
        self.feedback_volume_slider.valueChanged.connect(self._feedback_volume_moved)
        self.device_segmented.changed.connect(lambda value: self.save_performance_settings())
        for combo in (self.compute_combo, self.cpu_threads_combo):
            combo.currentIndexChanged.connect(lambda index=0: self.save_performance_settings())
        for row in (
            self.keep_model_loaded_row,
            self.low_ram_row,
            self.low_vram_row,
            self.fallback_cpu_row,
            self.auto_download_row,
            self.warn_large_row,
        ):
            row.switch.toggled.connect(lambda checked=False: self.save_performance_settings())
        settings = self.settings_window
        settings.start_with_windows.switch.toggled.connect(lambda checked=False: self.save_startup_settings())
        settings.keep_in_tray.switch.toggled.connect(lambda checked=False: self.save_tray_setting())
        settings.debug_log.switch.toggled.connect(lambda checked=False: self.save_debug_log_setting())
        settings.check_updates_button.clicked.connect(lambda checked=False: self.check_for_updates())
        settings.copy_diagnostics_button.clicked.connect(lambda checked=False: self.copy_diagnostics())
        settings.save_diagnostics_button.clicked.connect(lambda checked=False: self.save_diagnostics())
        settings.export_settings_button.clicked.connect(lambda checked=False: self.export_settings())
        settings.import_settings_button.clicked.connect(lambda checked=False: self.import_settings())
        settings.run_setup_button.clicked.connect(lambda checked=False: self.show_first_run_setup(force=True))
        settings.open_settings_folder_button.clicked.connect(
            lambda checked=False: self._open_folder(self.settings_manager.config_dir)
        )
        settings.open_model_folder_button.clicked.connect(
            lambda checked=False: self._open_folder(self.model_manager.model_storage_path())
        )
        self.level_changed.connect(self.update_level)
        self.transcription_done.connect(self.finish_transcription)
        self.transcription_failed.connect(self.fail_transcription)
        self.insertion_done.connect(self.finish_insertion)
        self.transcription_timeout.connect(self.handle_transcription_timeout)
        self.model_ready.connect(self.finish_model_prepare)
        self.model_failed.connect(self.fail_model_prepare)
        self.model_prepare_timeout.connect(self.handle_model_prepare_timeout)
        self.warm_up_done.connect(self.finish_warm_up)
        self.update_checked.connect(self.finish_update_check)

    def _load_settings_into_ui(self) -> None:
        self._loading_ui = True
        try:
            self.refresh_microphones()
            get = self.settings_manager.get
            self.dictation_toggle.switch.setChecked(True)
            self.shortcut_input.setText(str(get("shortcut", "Ctrl+Alt+Q")))
            self.mode_segmented.set_value(str(get("shortcut_mode", "hold")))
            self.restore_clipboard_row.switch.setChecked(bool(get("restore_clipboard", True)))
            self.add_space_row.switch.setChecked(bool(get("add_space_after_text", False)))
            self.press_enter_row.switch.setChecked(bool(get("press_enter_after_text", False)))
            self.capitalize_row.switch.setChecked(bool(get("auto_capitalization", False)))
            self.period_row.switch.setChecked(bool(get("auto_punctuation", False)))
            self.feedback_volume_slider.setValue(round(float(get("feedback_volume", 0.5)) * 100))
            self._feedback_volume_moved(self.feedback_volume_slider.value())
            settings = self.settings_window
            settings.start_with_windows.switch.setChecked(bool(get("start_with_windows", False)))
            settings.keep_in_tray.switch.setChecked(bool(get("minimize_to_tray", True)))
            settings.debug_log.switch.setChecked(bool(get("debug_logs", False)))
            self._restore_performance_controls()
        finally:
            self._loading_ui = False
        self.update_shortcut_warning()
        self.update_bottom_labels()

    def _restore_performance_controls(self) -> None:
        was_loading = self._loading_ui
        self._loading_ui = True
        try:
            get = self.settings_manager.get
            device = str(get("device", "auto"))
            if device == "auto" and not bool(get("use_gpu_if_available", True)):
                device = "cpu"
            self.device_segmented.set_value(device)
            self._select_combo_data(self.compute_combo, get("compute_type", "auto"))
            self._select_combo_data(self.cpu_threads_combo, get("cpu_threads", "auto"))
            self.fallback_cpu_row.switch.setChecked(bool(get("fallback_to_cpu", True)))
            self.low_vram_row.switch.setChecked(bool(get("low_vram_mode", False)))
            self.keep_model_loaded_row.switch.setChecked(bool(get("keep_model_loaded", True)))
            self.low_ram_row.switch.setChecked(bool(get("low_ram_mode", False)))
            self.auto_download_row.switch.setChecked(bool(get("auto_download_models", True)))
            self.warn_large_row.switch.setChecked(bool(get("warn_before_large_models", True)))
        finally:
            self._loading_ui = was_loading

    def _resolve_saved_microphone(self) -> str:
        """The saved microphone's id right now, or "" if it is gone.

        Device indexes shift when microphones are plugged in or removed, so the
        saved name decides which index is the same microphone.
        """
        saved_id = str(self.settings_manager.get("microphone_device", "default"))
        if saved_id == "default":
            return "default"
        name = str(self.settings_manager.get("microphone_name", ""))
        device = self.audio_recorder.input_device_by_id(saved_id)
        if not name:  # saved before names were kept: trust the index once and remember its name
            if device is not None:
                self.settings_manager.set("microphone_name", device.name)
                return saved_id
            return ""
        if device is not None and device.name == name:
            return saved_id
        for candidate in self.audio_recorder.list_input_devices(keep=None):
            if candidate.id != "default" and candidate.name == name:
                self.settings_manager.set("microphone_device", candidate.id)
                return candidate.id
        return ""

    def refresh_microphones(self) -> None:
        current = self._resolve_saved_microphone() or "missing"
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        for device in self.audio_recorder.list_input_devices(keep=current):
            self.mic_combo.addItem(device.name, device.id)
        index = self.mic_combo.findData(current)
        if index < 0 and current != "default":
            self.settings_manager.set("microphone_device", "default")
            self._append_activity_log("saved microphone missing; using the default")
            self.set_status(
                "Microphone changed",
                "The saved microphone was not found, so the default system microphone will be used.",
            )
        self.mic_combo.setCurrentIndex(index if index >= 0 else 0)
        self.mic_combo.blockSignals(False)
        self.update_bottom_labels()

    # ------------------------------------------------------------ model state

    def _pipeline_busy(self) -> bool:
        return self._is_listening or self._is_transcribing or self._is_preparing_model

    def _pipeline_busy_message(self) -> str:
        return "Wait until the current recording, transcription, or model preparation finishes."

    def _highlighted_model(self) -> str:
        item = self.model_list.currentItem()
        if item is None:
            return self.model_manager.selected_model()
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _show_model_details(self) -> None:
        name = self._highlighted_model()
        info = MODEL_INFO[name]
        active = name == self.model_manager.selected_model()
        installed = self.model_manager.is_model_ready(name)
        self.detail_title.setText(info.label)
        self.detail_languages.setText(f"{ENGINE_LABELS[info.engine]} · {info.languages}")
        self.detail_use.setText(info.recommended_use)
        self.speed_meter.set_value(SPEED_LEVELS.get(info.speed_estimate, 3))
        self.accuracy_meter.set_value(ACCURACY_LEVELS.get(info.accuracy_estimate, 3))
        self.detail_needs.setText(f"{info.approximate_size} · {info.resource_usage_estimate}")
        if self._is_preparing_model and active:
            text, enabled = "Preparing...", False
        elif active and installed:
            text, enabled = "In use", False
        elif installed:
            text, enabled = "Use this model", True
        else:
            text, enabled = f"Download and use ({info.approximate_size})", True
        self.use_model_button.setText(text)
        self.use_model_button.setEnabled(enabled)

    def use_highlighted_model(self) -> None:
        name = self._highlighted_model()
        if name == self.model_manager.selected_model():
            self.prepare_selected_model(auto=False)
        else:
            self.select_model(name)

    def _sync_model_list(self) -> None:
        selected = self.model_manager.selected_model()
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == selected:
                self.model_list.blockSignals(True)
                self.model_list.setCurrentItem(item)
                self.model_list.blockSignals(False)
        self.model_list.viewport().update()
        self._show_model_details()

    def _refresh_model_status(self) -> None:
        selected = self.model_manager.selected_model()
        info = self.model_manager.selected_info()
        self.settings_window.open_model_folder_button.setToolTip(
            str(self.model_manager.model_storage_path())
        )
        if not self._is_preparing_model:
            if self.model_manager.is_model_ready(selected):
                loaded = " and loaded" if self.transcriber.is_loaded() else ""
                self.model_status.setText(f"{info.label} is installed{loaded}.")
            elif bool(self.settings_manager.get("auto_download_models", True)):
                self.model_status.setText(
                    f"{info.label} downloads the first time you use the shortcut."
                )
            else:
                self.model_status.setText(f"{info.label} is not downloaded yet.")
        self._sync_model_list()
        self.update_bottom_labels()

    def _cancel_transcription_timer(self) -> None:
        if self._transcription_timer is not None:
            self._transcription_timer.cancel()
            self._transcription_timer = None

    def _start_transcription_timer(self, request_id: int, audio_seconds: float = 0.0) -> None:
        self._cancel_transcription_timer()
        # Long recordings and slow models legitimately take longer: allow 2 s per second of audio on top.
        timer = threading.Timer(
            TRANSCRIPTION_TIMEOUT_SECONDS + 2 * audio_seconds,
            lambda: self.transcription_timeout.emit(request_id),
        )
        timer.daemon = True
        self._transcription_timer = timer
        timer.start()

    def _cancel_model_prepare_timer(self) -> None:
        if self._model_prepare_timer is not None:
            self._model_prepare_timer.cancel()
            self._model_prepare_timer = None

    def _start_model_prepare_timer(self, request_id: int) -> None:
        self._cancel_model_prepare_timer()
        timer = threading.Timer(
            MODEL_PREPARE_TIMEOUT_SECONDS, lambda: self.model_prepare_timeout.emit(request_id)
        )
        timer.daemon = True
        self._model_prepare_timer = timer
        timer.start()

    def _reset_transcriber(self) -> None:
        self._warm_up_generation += 1
        self.transcriber = Transcriber(self.model_manager)

    def _schedule_warm_up(self) -> None:
        self._warm_up_timer.start()

    def reset_speech_engine(self) -> None:
        if self._pipeline_busy():
            self.set_status("Busy", self._pipeline_busy_message())
            return
        self._reset_transcriber()
        self._append_activity_log("speech engine reset")
        self._warm_up_timer.stop()
        self.warm_up_selected_model()
        self._refresh_model_status()
        self.set_status("Ready", "The speech engine was reset and is loading again.")

    def warm_up_selected_model(self) -> None:
        """Load the installed model in the background so the first dictation is instant.

        Recording stays available meanwhile; a dictation that finishes first
        simply waits for the load inside the transcriber.
        """
        settings = self.settings_manager.all()
        if (
            self._pipeline_busy()
            or self.transcriber.is_loaded()
            or not self.model_manager.is_model_ready()
            or not bool(settings.get("keep_model_loaded", True))
            or bool(settings.get("low_ram_mode", False))
        ):
            return
        self._warm_up_generation += 1
        generation = self._warm_up_generation
        transcriber = self.transcriber
        self._append_activity_log(f"warm-up start: {self.model_manager.selected_model()}")
        self.model_status.setText(f"Loading {self.model_manager.selected_info().label} into memory...")
        threading.Thread(
            target=self._warm_up_worker, args=(generation, transcriber, settings), daemon=True
        ).start()

    def _warm_up_worker(
        self, generation: int, transcriber: Transcriber, settings: dict[str, Any]
    ) -> None:
        started = time.perf_counter()
        try:
            transcriber.prepare_selected_model(settings)
        except Exception as exc:
            self.warm_up_done.emit(generation, str(exc) or exc.__class__.__name__)
        else:
            self._append_activity_log(f"warm-up done in {time.perf_counter() - started:.1f}s")
            self.warm_up_done.emit(generation, "")

    def finish_warm_up(self, generation: int, error: str) -> None:
        if generation != self._warm_up_generation:
            return
        if error:
            self._append_activity_log(f"warm-up failed: {error}")
        self._refresh_model_status()
        if error and not self._pipeline_busy():
            self.set_status("Ready", f"The model will load on first use. ({error})")

    def select_model(self, model_name: str, prepare: bool = True) -> None:
        if self._pipeline_busy():
            self._sync_model_list()
            self.set_status("Busy", self._pipeline_busy_message())
            return
        normalized = self.model_manager.normalize_model_name(model_name)
        if normalized != self.model_manager.selected_model() and not self._confirm_model_choice(
            normalized
        ):
            self._sync_model_list()
            return
        self.model_manager.select_model(normalized)
        self._reset_transcriber()
        self.model_changed.emit(normalized)
        if prepare and not self.model_manager.is_model_ready():
            self.prepare_selected_model(auto=True)
        else:
            self._schedule_warm_up()
        self._refresh_model_status()
        self.set_status("Ready", f"Using {self.model_manager.selected_info().label}. {self._ready_detail()}")

    def _confirm_model_choice(self, model_name: str) -> bool:
        warnings: list[str] = []
        if self.warn_large_row.switch.isChecked() and model_name in LARGE_MODELS:
            warnings.append(
                "Large local speech models need much more RAM or VRAM and can take longer to load."
            )
        if self.low_ram_row.switch.isChecked() and model_name in LARGE_MODELS:
            warnings.append("Low RAM mode is on, so a smaller model is safer.")
        if self.low_vram_row.switch.isChecked() and model_name in {"large", "large-v2", "large-v3"}:
            warnings.append("Low VRAM mode is on, so large models may be slow or fail on smaller GPUs.")
        if not warnings:
            return True
        return show_message(
            self,
            "Load a larger model?",
            " ".join(warnings),
            ("Load it", "Cancel"),
        )

    def prepare_selected_model(self, auto: bool) -> None:
        if self._is_preparing_model:
            return
        if self._is_listening or self._is_transcribing:
            if not auto:
                self.set_status("Busy", self._pipeline_busy_message())
            return
        selected = self.model_manager.selected_model()
        info = self.model_manager.selected_info()
        if self.model_manager.is_model_ready():
            if not auto:
                self._warm_up_timer.stop()
                self.warm_up_selected_model()
            self._refresh_model_status()
            return
        if auto and not bool(self.settings_manager.get("auto_download_models", True)):
            self._refresh_model_status()
            return
        if not auto and not bool(self.settings_manager.get("auto_download_models", True)):
            if not show_message(
                self,
                "Download speech model?",
                f"{info.label} is not downloaded yet ({info.approximate_size}). Download it now?",
                ("Download", "Not now"),
            ):
                return
        if self.model_manager.is_large_model() and bool(
            self.settings_manager.get("warn_before_large_models", True)
        ):
            if not self._confirm_model_choice(selected):
                return

        self._is_preparing_model = True
        self._model_prepare_started_at = time.monotonic()
        self._model_prepare_request_id += 1
        request_id = self._model_prepare_request_id
        self._active_model_prepare_id = request_id
        self.model_progress.setVisible(True)
        if info.engine == "voz":
            self.model_progress.setRange(0, 100)
            self.model_progress.setValue(0)
        else:
            self.model_progress.setRange(0, 0)
        self._tick_model_progress()
        self._model_progress_timer.start()
        self._refresh_visual_state()
        self._sync_model_list()
        settings = self.settings_manager.all()
        transcriber = self.transcriber
        self._start_model_prepare_timer(request_id)
        self._append_activity_log(f"model prepare start: {selected}")
        threading.Thread(
            target=self._prepare_model_worker,
            args=(request_id, settings, transcriber),
            daemon=True,
        ).start()

    def _prepare_model_worker(
        self, request_id: int, settings: dict[str, Any], transcriber: Transcriber
    ) -> None:
        try:
            transcriber.prepare_selected_model(settings)
        except Exception as exc:
            self.model_failed.emit(request_id, str(exc))
        else:
            self.model_ready.emit(request_id, self.model_manager.selected_model())

    def _end_model_prepare(self) -> None:
        self._cancel_model_prepare_timer()
        self._active_model_prepare_id = 0
        self._is_preparing_model = False
        self.model_progress.setVisible(False)
        self._model_progress_timer.stop()

    def finish_model_prepare(self, request_id: int, model_name: str) -> None:
        if request_id != self._active_model_prepare_id:
            return
        self._end_model_prepare()
        self._append_activity_log(f"model prepare done: {model_name}")
        self._refresh_model_status()
        self.set_status("Ready", self.transcriber.runtime_message or self._ready_detail())

    def fail_model_prepare(self, request_id: int, message: str) -> None:
        if request_id != self._active_model_prepare_id:
            return
        self._end_model_prepare()
        self._reset_transcriber()
        self._append_activity_log(f"model prepare failed: {message}")
        self._refresh_model_status()
        self.model_status.setText(message)
        self.set_status("Error", message)

    def handle_model_prepare_timeout(self, request_id: int) -> None:
        if request_id != self._active_model_prepare_id or not self._is_preparing_model:
            return
        self._end_model_prepare()
        self._reset_transcriber()
        message = "Model preparation took too long and was stopped. Check your connection, or pick a smaller model."
        self._append_activity_log("model prepare timeout")
        self._refresh_model_status()
        self.model_status.setText(message)
        self.set_status("Error", message)

    def _tick_model_progress(self) -> None:
        if not self._is_preparing_model:
            self._model_progress_timer.stop()
            return
        elapsed = int(max(0, time.monotonic() - self._model_prepare_started_at))
        info = self.model_manager.selected_info()
        if info.engine == "voz":
            total = sum(voz_engine.BUNDLE_FILES.values())
            have = min(total, _folder_bytes(self.model_manager.model_storage_path() / "voz"))
            if voz_engine.missing_files(self.model_manager.model_storage_path()):
                self.model_progress.setValue(int(100 * have / total))
                detail = f"Downloading {info.label}: {have // 1_000_000} of {total // 1_000_000} MB ({elapsed}s)."
            else:
                self.model_progress.setValue(100)
                detail = f"Optimizing and loading {info.label} for this CPU ({elapsed}s)."
        else:
            detail = f"Preparing {info.label}: downloading or loading, {elapsed}s so far."
        self.model_status.setText(detail)
        self.set_status("Preparing", detail)

    # --------------------------------------------------------------- dictation

    def toggle_manual_listening(self) -> None:
        if self._is_listening:
            self.stop_listening_and_transcribe()
        else:
            self.start_listening()

    def _append_activity_log(self, message: str) -> None:
        app_log.log(message)

    def _dictation_toggled(self, enabled: bool) -> None:
        if self._loading_ui:
            return
        self.hotkey_enabled_changed.emit(enabled)
        self.set_status(
            "Ready" if enabled else "Paused",
            self._ready_detail() if enabled else "The shortcut is paused. Turn Dictation on to use it again.",
        )

    def start_listening(self) -> None:
        if self._is_capturing_shortcut:
            self._append_activity_log("start ignored: shortcut capture active")
            return
        if self._is_listening or self._is_transcribing:
            self._append_activity_log("start ignored: already busy")
            return
        if self._is_preparing_model:
            self.set_status(
                "Preparing", "The speech model is still being prepared. Try again when it says Ready."
            )
            return
        if not self.model_manager.is_model_ready():
            self._append_activity_log("start triggered model prepare")
            self.prepare_selected_model(auto=True)
            if not self._is_preparing_model:
                self.set_status(
                    "Model not ready",
                    "Automatic downloads are off. Pick the model on the left and click Download and use.",
                )
            return

        microphone_id = self._selected_microphone_id()
        current_target = self.text_inserter.get_foreground_window()
        own_window_hwnd = int(self.winId())
        if current_target and current_target.window_hwnd != own_window_hwnd:
            self._target_hwnd = current_target
        else:
            self._target_hwnd = None
        try:
            self.audio_recorder.start(str(microphone_id), self.level_changed.emit)
        except AudioRecorderError as exc:
            self._append_activity_log("microphone start failed")
            self.set_status("Error", str(exc))
            return
        self._append_activity_log("listening")
        self._is_listening = True
        mode = str(self.settings_manager.get("shortcut_mode", "hold"))
        self.set_status(
            "Listening",
            "Speak now. Release the shortcut to type it."
            if mode == "hold"
            else "Speak now. Press the shortcut again to type it.",
        )

    def _selected_microphone_id(self) -> str:
        microphone_id = self._resolve_saved_microphone()
        if microphone_id:
            return microphone_id
        self._append_activity_log("microphone missing before recording; using default")
        self.settings_manager.set("microphone_device", "default")
        self.refresh_microphones()
        return "default"

    def stop_listening_and_transcribe(self, release_window_hwnd: object = None) -> None:
        if not self._is_listening:
            return
        self._is_listening = False
        try:
            release_window = int(release_window_hwnd) if release_window_hwnd is not None else None
        except (TypeError, ValueError):
            release_window = None
        if release_window:
            captured_target = self.text_inserter.get_window_target(release_window)
            if captured_target is not None:
                self._target_hwnd = captured_target
        try:
            audio = self.audio_recorder.stop()
        except AudioRecorderError as exc:
            self._append_activity_log("microphone stop failed")
            self.set_status("Error", str(exc))
            return

        self._is_transcribing = True
        self._transcription_request_id += 1
        request_id = self._transcription_request_id
        self._active_transcription_id = request_id
        self._append_activity_log(f"captured {audio.size / SAMPLE_RATE:.1f}s of audio")
        self.set_status("Transcribing", "Turning your speech into text on this PC.")
        settings = self.settings_manager.all()
        self._start_transcription_timer(request_id, audio.size / SAMPLE_RATE)
        threading.Thread(
            target=self._transcribe_worker,
            args=(request_id, audio, settings, self.transcriber),
            daemon=True,
        ).start()

    def cancel_listening(self) -> None:
        if not self._is_listening:
            return
        self._is_listening = False
        self.audio_recorder.cancel()
        self._append_activity_log("recording cancelled")
        self.set_status("Ready", "Recording cancelled.")

    def _transcribe_worker(
        self,
        request_id: int,
        audio: np.ndarray,
        settings: dict[str, Any],
        transcriber: Transcriber,
    ) -> None:
        seconds = audio.size / SAMPLE_RATE
        started = time.perf_counter()
        try:
            text = transcriber.transcribe(audio, settings)
        except Exception as exc:
            self._append_activity_log(f"transcription failed: id={request_id} {exc}")
            self.transcription_failed.emit(request_id, str(exc))
            return
        elapsed = time.perf_counter() - started
        # Only sizes and timings are logged, never the words.
        self._append_activity_log(
            f"transcribed id={request_id}: {seconds:.1f}s audio in {elapsed:.2f}s, {len(text)} chars"
        )
        self.transcription_done.emit(request_id, text, seconds, elapsed)

    def finish_transcription(
        self, request_id: int, text: str, audio_seconds: float, elapsed: float
    ) -> None:
        if request_id != self._active_transcription_id:
            return
        self._cancel_transcription_timer()
        self._active_transcription_id = 0
        self._is_transcribing = False
        self._show_last_dictation(text, audio_seconds, elapsed)
        if not text:
            self.set_status(
                "Ready",
                self.transcriber.runtime_message
                or "No speech was detected. Try speaking closer to the microphone.",
            )
            self._target_hwnd = None
            return
        self.last_transcript.setPlainText(text)
        self.set_status("Typing", "Typing into the app you were using.")
        # Typing can take seconds in games, so it runs off the UI thread; stay busy
        # until it is done so a new recording cannot start in the middle of it.
        self._is_transcribing = True
        target, self._target_hwnd = self._target_hwnd, None
        threading.Thread(
            target=self._insert_worker,
            args=(text, target, self.settings_manager.all()),
            daemon=True,
        ).start()

    def _insert_worker(self, text: str, target: TextTarget | None, settings: dict[str, Any]) -> None:
        try:
            self.text_inserter.insert_text(text, target, settings)
        except TextInsertionError as exc:
            self.insertion_done.emit(str(exc))
            return
        except Exception as exc:
            self.insertion_done.emit(f"Could not type the text: {exc}")
            return
        self.insertion_done.emit("")

    def finish_insertion(self, error: str) -> None:
        self._is_transcribing = False
        if error:
            self._append_activity_log("text insertion failed")
            self.set_status("Error", error)
            return
        self._append_activity_log("text inserted")
        self.set_status("Ready", self.transcriber.runtime_message or self._ready_detail(done=True))

    def _show_last_dictation(self, text: str, audio_seconds: float, elapsed: float) -> None:
        info = self.model_manager.selected_info()
        speed = audio_seconds / elapsed if elapsed > 0 else 0.0
        self.last_value.setText(f"{elapsed:.2f} s")
        self.last_condition.setText(
            f"{audio_seconds:.1f} s of speech · {len(text)} characters" if text else "No speech detected"
        )
        self.last_detail.setText(f"{info.label} · {speed:.0f}x faster than real time")

    def fail_transcription(self, request_id: int, message: str) -> None:
        if request_id != self._active_transcription_id:
            return
        self._cancel_transcription_timer()
        self._active_transcription_id = 0
        self._is_transcribing = False
        self.set_status("Error", message)
        self._target_hwnd = None

    def handle_transcription_timeout(self, request_id: int) -> None:
        if request_id != self._active_transcription_id or not self._is_transcribing:
            return
        self._cancel_transcription_timer()
        self._active_transcription_id = 0
        self._is_transcribing = False
        self._target_hwnd = None
        self._append_activity_log(f"transcription timeout id={request_id}")
        self._reset_transcriber()
        self.set_status(
            "Error",
            "Transcription took too long, so the speech engine was reset. Try again, or pick a faster model.",
        )

    def copy_transcript(self) -> None:
        text = self.last_transcript.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.set_status("Ready", "The last transcription was copied to the clipboard.")

    # --------------------------------------------------------------- settings

    def apply_shortcut_settings(self) -> None:
        shortcut = self.shortcut_input.text().strip() or "Ctrl+Alt+Q"
        try:
            parse_shortcut(shortcut)
        except ValueError as exc:
            show_message(self, "Shortcut problem", str(exc))
            return
        warning = shortcut_warning(shortcut)
        saved = str(self.settings_manager.get("shortcut", ""))
        if warning and shortcut != saved:
            if not show_message(
                self, "Shortcut warning", f"{warning} Save this shortcut anyway?", ("Save it", "Cancel")
            ):
                self.shortcut_input.setText(saved)
                self.set_status("Shortcut not saved", "Choose a safer shortcut or keep Ctrl+Alt+Q.")
                return
        mode = self.mode_segmented.value()
        self.shortcut_changed.emit(shortcut, mode)
        self.settings_manager.update({"shortcut": shortcut, "shortcut_mode": mode})
        self.update_bottom_labels()
        self.set_status("Ready", f"Shortcut saved: {shortcut}, {self._mode_label().lower()} to talk.")

    def start_shortcut_capture(self) -> None:
        self._is_capturing_shortcut = True
        self.set_status("Shortcut capture", "Press the shortcut you want to use. Press Esc to cancel.")

    def finish_shortcut_capture(self, shortcut: str) -> None:
        self._is_capturing_shortcut = False
        self.shortcut_input.setText(shortcut)
        self.apply_shortcut_settings()

    def cancel_shortcut_capture(self) -> None:
        self._is_capturing_shortcut = False
        self.set_status("Ready", "Shortcut change cancelled.")

    def save_microphone_setting(self) -> None:
        if self._loading_ui:
            return
        device_id = str(self.mic_combo.currentData() or "default")
        self.settings_manager.update(
            {
                "microphone_device": device_id,
                "microphone_name": "" if device_id == "default" else self.mic_combo.currentText(),
            }
        )
        self.update_bottom_labels()

    def save_typing_settings(self) -> None:
        if self._loading_ui:
            return
        self.settings_manager.update(
            {
                "insert_method": "clipboard_paste",
                "restore_clipboard": self.restore_clipboard_row.switch.isChecked(),
                "add_space_after_text": self.add_space_row.switch.isChecked(),
                "press_enter_after_text": self.press_enter_row.switch.isChecked(),
                "auto_capitalization": self.capitalize_row.switch.isChecked(),
                "auto_punctuation": self.period_row.switch.isChecked(),
            }
        )

    def _feedback_volume_moved(self, value: int) -> None:
        self.feedback_volume_value.setText(f"{value}%" if value else "Off")
        if not self._loading_ui:
            self._feedback_volume_timer.start()

    def save_feedback_volume(self) -> None:
        volume = self.feedback_volume_slider.value() / 100
        self.settings_manager.set("feedback_volume", volume)
        self.feedback_volume_changed.emit(volume)

    def save_startup_settings(self) -> None:
        if self._loading_ui:
            return
        switch = self.settings_window.start_with_windows.switch
        enabled = switch.isChecked()
        try:
            self.startup_manager.set_enabled(enabled)
        except StartupManagerError as exc:
            switch.blockSignals(True)
            switch.setChecked(not enabled)
            switch.blockSignals(False)
            self.set_status("Error", str(exc))
            return
        self.settings_manager.set("start_with_windows", enabled)
        self.set_status(
            "Ready",
            "Whisper Anywhere will start hidden in the tray when Windows starts."
            if enabled
            else "Whisper Anywhere will not start with Windows.",
        )

    def save_tray_setting(self) -> None:
        if not self._loading_ui:
            self.settings_manager.set(
                "minimize_to_tray", self.settings_window.keep_in_tray.switch.isChecked()
            )

    def save_debug_log_setting(self) -> None:
        if self._loading_ui:
            return
        enabled = self.settings_window.debug_log.switch.isChecked()
        self.settings_manager.set("debug_logs", enabled)
        app_log.write_to_disk(self.settings_manager.debug_log_path if enabled else None)
        self.set_status(
            "Ready",
            f"Debug log: {self.settings_manager.debug_log_path}" if enabled else "Debug log is off.",
        )

    def save_performance_settings(self) -> None:
        if self._loading_ui:
            return
        if self._pipeline_busy():
            self._restore_performance_controls()
            self.set_status("Busy", self._pipeline_busy_message())
            return
        device = self.device_segmented.value()
        self.settings_manager.update(
            {
                # The old preset menu is gone; its effects live in the toggles.
                "performance_preset": "balanced",
                "device": device,
                "use_gpu_if_available": device != "cpu",
                "compute_type": str(self.compute_combo.currentData() or "auto"),
                "cpu_threads": self.cpu_threads_combo.currentData() or "auto",
                "low_ram_mode": self.low_ram_row.switch.isChecked(),
                "low_vram_mode": self.low_vram_row.switch.isChecked(),
                "fallback_to_cpu": self.fallback_cpu_row.switch.isChecked(),
                "warn_before_large_models": self.warn_large_row.switch.isChecked(),
                "auto_download_models": self.auto_download_row.switch.isChecked(),
                "keep_model_loaded": self.keep_model_loaded_row.switch.isChecked(),
            }
        )
        self._reset_transcriber()
        self._refresh_model_status()
        self._schedule_warm_up()
        self.set_status("Ready", "Performance settings saved.")

    def show_first_run_setup(self, force: bool = False) -> None:
        if not force and bool(self.settings_manager.get("first_run_setup_completed", False)):
            return
        dialog = FirstRunSetupDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            if not force:
                self.settings_manager.set("first_run_setup_completed", True)
            self.set_status("Ready", "Setup skipped. Current settings are unchanged.")
            return
        shortcut = dialog.shortcut_input.text().strip() or "Ctrl+Alt+Q"
        try:
            parse_shortcut(shortcut)
        except ValueError as exc:
            show_message(self, "Shortcut problem", str(exc))
            return
        device = str(dialog.device_combo.currentData() or "cpu")
        self.settings_manager.update(
            {
                "shortcut": shortcut,
                "shortcut_mode": "hold",
                "selected_model": str(dialog.model_combo.currentData() or "voz"),
                "microphone_device": str(dialog.mic_combo.currentData() or "default"),
                "device": device,
                "compute_type": "auto",
                "use_gpu_if_available": device != "cpu",
                "first_run_setup_completed": True,
            }
        )
        self.shortcut_changed.emit(shortcut, "hold")
        self.model_changed.emit(self.model_manager.selected_model())
        self._reset_transcriber()
        self._load_settings_into_ui()
        self._refresh_model_status()
        self.prepare_selected_model(auto=True)
        self._schedule_warm_up()
        self.set_status("Ready", f"Setup saved. {self._ready_detail()}")

    # ------------------------------------------------------- updates, support

    def check_for_updates(self) -> None:
        self.settings_window.check_updates_button.setEnabled(False)
        self.set_status("Checking updates", "Checking the latest GitHub release. Nothing is installed automatically.")
        threading.Thread(target=self._check_for_updates_worker, daemon=True).start()

    def _check_for_updates_worker(self) -> None:
        try:
            request = urllib.request.Request(
                GITHUB_RELEASES_API, headers={"User-Agent": "Whisper Anywhere"}
            )
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
            latest = str(payload.get("tag_name") or payload.get("name") or "")
            url = str(payload.get("html_url") or GITHUB_RELEASES_URL)
            if not latest:
                raise ValueError("GitHub did not return a release tag.")
            self.update_checked.emit(_version_tuple(latest) > _version_tuple(APP_VERSION), latest, url)
        except Exception as exc:
            self.update_checked.emit(False, "", str(exc))

    def finish_update_check(self, has_update: bool, latest: str, detail: str) -> None:
        self.settings_window.check_updates_button.setEnabled(True)
        version = self.settings_window.version_label
        if not latest:
            self.set_status("Update check failed", detail)
            return
        if has_update:
            version.setText(f"Version {APP_VERSION} · {latest} is available")
            self.set_status("Update available", f"Release {latest} is out. Updates are manual downloads.")
            if show_message(
                self,
                "Update available",
                f"Installed: {APP_VERSION}. Latest: {latest}. Open the download page?",
                ("Open page", "Later"),
            ):
                webbrowser.open(detail)
        else:
            version.setText(f"Version {APP_VERSION} · up to date")
            self.set_status("Up to date", f"Version {APP_VERSION} is the latest release.")

    def _recent_safe_log_lines(self, max_lines: int = 40) -> list[str]:
        return [_sanitize_path_text(line) for line in app_log.recent(max_lines)]

    def diagnostics_text(self) -> str:
        microphone_id = str(self.settings_manager.get("microphone_device", "default"))
        microphone = self.audio_recorder.input_device_by_id(
            microphone_id
        ) or self.audio_recorder.input_device_by_id("default")
        payload = {
            "app": "Whisper Anywhere",
            "version": APP_VERSION,
            "settings": self.settings_manager.portable_export(),
            "runtime": {
                "model": self.model_manager.selected_model(),
                "model_installed": self.model_manager.is_model_ready(),
                "model_loaded": self.transcriber.is_loaded(),
                "device": self.settings_manager.get("device", "cpu"),
                "compute_type": self.settings_manager.get("compute_type", "int8"),
                "cpu_threads": self.settings_manager.get("cpu_threads", "auto"),
                "logical_cpus": os.cpu_count(),
            },
            "hotkey_status": self.hotkey_status_label.text(),
            "selected_microphone": {
                "id": microphone_id if microphone_id == "default" else "<device-id>",
                "name": microphone.name if microphone else "Default system microphone",
            },
            "recent_logs": self._recent_safe_log_lines(),
        }
        return json.dumps(payload, indent=2)

    def copy_diagnostics(self) -> None:
        QApplication.clipboard().setText(self.diagnostics_text())
        self.set_status("Diagnostics copied", "Diagnostics (no transcripts, no personal paths) are on the clipboard.")

    def save_diagnostics(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save diagnostics", "Whisper-Anywhere-Diagnostics.json", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self.diagnostics_text() + "\n", encoding="utf-8")
        except OSError as exc:
            show_message(self, "Diagnostics save failed", str(exc))
            return
        self.set_status("Diagnostics saved", "Diagnostics were saved.")

    def export_settings(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export settings", "Whisper-Anywhere-Settings.json", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            Path(path).write_text(
                json.dumps(self.settings_manager.portable_export(), indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            show_message(self, "Settings export failed", str(exc))
            return
        self.set_status("Settings exported", "Settings were exported without personal folder paths.")

    def import_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import settings", "", "JSON files (*.json)")
        if not path:
            return
        try:
            values = json.loads(Path(path).read_text(encoding="utf-8"))
            ignored = self.settings_manager.import_portable(values)
        except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
            show_message(self, "Settings import failed", f"Could not import settings: {exc}")
            return
        self._load_settings_into_ui()
        self._reset_transcriber()
        self._refresh_model_status()
        self.shortcut_changed.emit(
            str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q")),
            str(self.settings_manager.get("shortcut_mode", "hold")),
        )
        self.model_changed.emit(self.model_manager.selected_model())
        self.feedback_volume_changed.emit(float(self.settings_manager.get("feedback_volume", 0.5)))
        self._schedule_warm_up()
        detail = "Settings imported."
        if ignored:
            detail += f" Ignored local or unknown fields: {', '.join(ignored[:6])}."
        self.set_status("Settings imported", detail)

    def _open_folder(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_settings_window(self) -> None:
        window = self.settings_window
        window.adjustSize()
        frame = self.frameGeometry()
        window.move(
            frame.center().x() - window.width() // 2,
            frame.top() + max(0, (frame.height() - window.height()) // 2),
        )
        window.show()
        window.raise_()
        window.activateWindow()

    # ------------------------------------------------------------------ status

    def update_shortcut_warning(self) -> None:
        if self._is_capturing_shortcut:
            self.shortcut_warning.setText("")
            return
        self.shortcut_warning.setText(
            shortcut_warning(self.shortcut_input.text().strip() or "Ctrl+Alt+Q")
        )

    def update_level(self, level: float) -> None:
        self.mic_button.set_level(level)
        self.horizon.push_level(level)

    def set_status(self, status: str, detail: str) -> None:
        # The state shows on the mic wheel and in the scene; only the
        # sentence underneath is text.
        self._status_is_error = status in ERROR_STATUSES
        self.status_detail.setText(detail)
        self.status_detail.setObjectName("error" if self._status_is_error else "muted")
        self.status_detail.style().unpolish(self.status_detail)
        self.status_detail.style().polish(self.status_detail)
        self._refresh_visual_state()

    def _visual_state(self) -> str:
        if self._is_listening:
            return "listening"
        if self._is_transcribing or self._is_preparing_model:
            return "busy"
        if self._status_is_error:
            return "error"
        return "idle"

    def _refresh_visual_state(self) -> None:
        state = self._visual_state()
        shortcut = str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q"))
        caption = {"listening": "Listening", "busy": "Working", "error": "Error"}.get(state, shortcut)
        self.mic_button.set_state(state, caption)
        self.horizon.set_mode(state)
        self.listen_button.setText("Stop and type" if state == "listening" else "Start listening")
        self.listen_button.setEnabled(state != "busy")
        tray_state = state if state in ("listening", "busy") else "idle"
        if tray_state != self._last_state:
            self._last_state = tray_state
            self.state_changed.emit(tray_state)

    def set_hotkey_status(self, message: str) -> None:
        self.hotkey_status_label.setText(message)

    def show_hotkey_error(self, message: str) -> None:
        self.set_hotkey_status(message)
        self.set_status("Hotkey error", message)
        show_message(self, "Hotkey error", message)

    def update_bottom_labels(self) -> None:
        self._refresh_visual_state()
        self.mic_button.setToolTip(
            f"{self._mode_label()} {self.settings_manager.get('shortcut', 'Ctrl+Alt+Q')} anywhere, or click here"
        )

    def _ready_detail(self, done: bool = False) -> str:
        shortcut = str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q"))
        verb = "hold" if self._mode_label() == "Hold" else "press"
        lead = "Done. " if done else ""
        return f"{lead}Click into any app, {verb} {shortcut} and speak."

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

    def open_settings(self) -> None:
        self.open_from_tray()
        self.open_settings_window()

    def exit_app(self) -> None:
        self._force_exit = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if bool(self.settings_manager.get("minimize_to_tray", True)) and not self._force_exit:
            event.ignore()
            self.settings_window.hide()
            self.hide()
            return
        self.settings_window.close()
        self.audio_recorder.close()
        event.accept()
