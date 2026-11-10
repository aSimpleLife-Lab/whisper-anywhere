from __future__ import annotations

import ctypes
import html
import json
import os
import re
import threading
import time
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QMimeData, QPoint, QRectF, QSize, Qt, QTimer, QUrl, Signal
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
    QStackedWidget,
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
from core.hotkey_listener import (
    TAP_SECONDS,
    parse_shortcut,
    same_shortcut,
    shortcut_problem,
    shortcut_warning,
)
from core.model_manager import LARGE_MODELS, MODEL_INFO, ModelManager, engine_installed
from core.settings_manager import SettingsManager
from core.startup_manager import StartupManager, StartupManagerError
from core.text_inserter import (
    PRIVATE_CLIPBOARD_FORMATS,
    InsertionCancelled,
    TextInserter,
    TextInsertionError,
    TextTarget,
)
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
# Nothing new on disk for this long means a model download or load is stuck.
MODEL_PREPARE_STALL_SECONDS = 300.0
WARM_UP_DELAY_MS = 1500
TOO_SHORT_SECONDS = 0.25  # a keyboard tap: nothing worth transcribing
# Typing gives up after this, plus a little per character for games.
INSERT_TIMEOUT_SECONDS = 20.0
INSERT_SECONDS_PER_CHAR = 0.02
RETYPE_KEEP_MS = 5 * 60 * 1000  # the last dictation can be typed again for 5 minutes
DEVICE_SETTLE_MS = 1500  # USB and Bluetooth changes arrive in bursts
UNLOAD_CHOICES = (
    ("Never", "never"),
    ("After 5 idle minutes", "5"),
    ("After 15 idle minutes", "15"),
    ("After 60 idle minutes", "60"),
    ("After every dictation", "each"),
)
INSERT_CHOICES = (("Auto", "auto"), ("Paste", "paste"), ("Type", "type"))
SEND_CHOICES = (("Default", "default"), ("Enter", "enter"), ("Ctrl+Enter", "ctrl+enter"), ("Nothing", "none"))
FULL_STOP_CHOICES = (("Default", "default"), ("On", "on"), ("Off", "off"))
NEW_PROFILE = {"insert_method": "auto", "send_key": "default", "full_stop": "default"}
# Messages the window watches for (see nativeEvent).
WM_DEVICECHANGE = 0x0219
DBT_DEVNODES_CHANGED = 0x0007
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0
ERROR_STATUSES = {"Error", "Not typed", "Hotkey error", "Start-up problem", "Shortcut problem", "Update check failed"}
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


def _copy_profiles(settings_manager: SettingsManager) -> dict[str, dict[str, str]]:
    saved = settings_manager.get("app_profiles", {}) or {}
    return {name: dict(profile) for name, profile in saved.items()} if isinstance(saved, dict) else {}


def _error_text(exc: BaseException) -> str:
    """An exception's message, or its name when it has none: a bare
    exception showed a blank red error."""
    return str(exc) or exc.__class__.__name__


def _linkify(text: str) -> str:
    """Rich text with every https link clickable; everything else escaped."""
    return re.sub(
        r"https://[^\s<]+[^\s<.,)]",
        lambda match: f'<a style="color:#55C7D8" href="{match.group(0)}">{match.group(0)}</a>',
        html.escape(text, quote=False),
    )


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


class _PrivateTextEdit(QPlainTextEdit):
    """Ctrl+C, right-click Copy and drags from here are marked private like a
    paste: no Win+V history, cloud clipboard or clipboard monitors."""

    def createMimeDataFromSelection(self) -> QMimeData:
        mime = super().createMimeDataFromSelection()
        for name, value in PRIVATE_CLIPBOARD_FORMATS.items():
            mime.setData(f'application/x-qt-windows-mime;value="{name}"', value)
        return mime


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
        elif self.window._model_ready.get(name, False):  # checked on refresh, not every repaint
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


def _tutorial_pages(shortcut: str, retype: str = "Ctrl+Alt+V") -> list[tuple[str, str]]:
    again = (
        f"If a dictation didn't arrive, {retype} types it again."
        if retype
        else "If a dictation didn't arrive, copy it from Last transcription."
    )
    return [
        (
            "Talk, let go, it types",
            f"Click into any text box: a chat, an email, a document or a game's chat. Hold {shortcut} and "
            "speak. When you let go, your words are typed where the cursor is.\n\nRather press once to start "
            "and again to stop? Pick Toggle next to the shortcut. Esc cancels, even after you let go, until "
            "the text is typed. A quick tap of a mouse-button shortcut stays a normal click.",
        ),
        (
            "Watch it listen",
            "While you speak, the voxel columns rise with your voice and the wheel counts the seconds. The "
            "cyan scanner means your words are being turned into text, usually in well under a second.\n\n"
            "The tray icon gets an orange dot while it listens, a cyan dot while it works and a red dot when "
            "something went wrong: point at it to read why. The Dictation on switch, here and in the tray "
            "menu, pauses the shortcut.",
        ),
        (
            "Speech models",
            "Voz is the recommended model: fast and accurate, with punctuation, for 25 European languages, and "
            "it needs no GPU. It downloads once, about 385 MB. For other languages, pick a Whisper model in the "
            "list on the left.\n\nIf a download cannot finish, the message under the list says why and links "
            "to the files, so you can download them in your browser instead.",
        ),
        (
            "Typing options and games",
            "The switches on the right decide how the text arrives: keep your clipboard, add a space, press "
            "Enter to send chat messages, capitalize, end with a full stop. App profiles in Settings change "
            "them for one app.\n\nIn full-screen games the words are typed in for you; open the game's chat "
            f"box before you speak. {again}",
        ),
        (
            "Sounds, privacy and the tray",
            "Sound effects sets the volume of the start, stop, cancel and error sounds; 0 turns them off. "
            "Recordings stay in memory and are never saved to disk.\n\nClosing the window keeps Whisper Anywhere in the tray, so "
            "the shortcut keeps working. The gear opens Settings: Start with Windows and diagnostics.\n\n"
            "This tour shows once. To see it again, click the orange ! at the top of the window.",
        ),
    ]


class TutorialDialog(ShellDialog):
    """A five-step tour; shown once after setup, replayed from the ! button or Settings."""

    def __init__(self, parent: "MainWindow") -> None:
        super().__init__(parent, "How Whisper Anywhere works", "Five quick steps.", width=500)
        shortcut = str(parent.settings_manager.get("shortcut", "Ctrl+Alt+Q"))
        retype = str(parent.settings_manager.get("retype_shortcut", "") or "")
        self.pages = QStackedWidget()
        for title, text in _tutorial_pages(shortcut, retype):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
            layout.addWidget(CubeTitle(title, "columnTitle"))
            layout.addWidget(label(text, "muted", wrap=True))
            layout.addStretch(1)
            self.pages.addWidget(page)
        self.pages.setMinimumHeight(190)
        self.body.addWidget(self.pages)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.dots: list[QLabel] = []
        for _ in range(self.pages.count()):
            dot = QLabel()
            dot.setFixedSize(10, 10)
            self.dots.append(dot)
            row.addWidget(dot)
        row.addStretch(1)
        self.back_button = button("Back")
        self.next_button = button("Next", "primary")
        self.back_button.clicked.connect(lambda checked=False: self._go(-1))
        self.next_button.clicked.connect(lambda checked=False: self._go(1))
        row.addWidget(self.back_button)
        row.addWidget(self.next_button)
        self.body.addLayout(row)
        self._go(0)

    def _go(self, step: int) -> None:
        index = self.pages.currentIndex() + step
        if index >= self.pages.count():
            self.accept()
            return
        index = max(0, index)
        self.pages.setCurrentIndex(index)
        for position, dot in enumerate(self.dots):  # voxel step dots: orange for this step
            dot.setStyleSheet(f"background: {'#EA8A2B' if position == index else '#2C2C2C'}; border-radius: 2px;")
        self.back_button.setEnabled(index > 0)
        self.next_button.setText("Done" if index == self.pages.count() - 1 else "Next")


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
        current = parent._resolve_saved_microphone() or "default"
        for device in parent.audio_recorder.list_input_devices(keep=current):
            self.mic_combo.addItem(device.name, device.id)
        parent._select_combo_data(self.mic_combo, current)
        form.addRow("Microphone", self.mic_combo)
        self.device_combo = SafeComboBox()
        self.device_combo.addItem("CPU (recommended)", "cpu")
        self.device_combo.addItem("Auto: GPU when available", "auto")
        self.device_combo.addItem("GPU preferred", "gpu")
        parent._select_combo_data(self.device_combo, parent.settings_manager.get("device", "cpu"))
        form.addRow("Hardware", self.device_combo)
        self.form = form
        self.body.addLayout(form)
        # After the form is in the dialog: showing a row of a form that has no
        # window yet makes Qt show its label and box as windows of their own.
        self.model_combo.currentIndexChanged.connect(self._show_hardware_row)
        self._show_hardware_row()
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

    def _show_hardware_row(self, _index: int = 0) -> None:
        """Voz always runs on the CPU: Hardware is asked only for a model that can use a GPU."""
        name = str(self.model_combo.currentData() or "voz")
        self.form.setRowVisible(self.device_combo, MODEL_INFO[name].engine != "voz")
        if self.isVisible():  # before the first show, Qt sizes it for all that is in it
            self.fit_content()


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
        self.startup_note = label("", "warning", wrap=True)
        self.startup_note.setVisible(False)
        for index, row in enumerate((self.start_with_windows, self.keep_in_tray, self.debug_log)):
            if index:
                system_layout.addWidget(divider())
            system_layout.addWidget(row)
            if row is self.start_with_windows:
                system_layout.addWidget(self.startup_note)
        self.body.addWidget(system)

        self._window = parent
        self.body.addWidget(CubeTitle("Type the last dictation again"))
        retype = QFrame()
        retype.setObjectName("glass")
        retype_layout = QVBoxLayout(retype)
        retype_layout.setContentsMargins(14, 10, 14, 12)
        retype_layout.setSpacing(8)
        retype_layout.addWidget(
            label(
                "For when the text didn't arrive: this shortcut types your last dictation into the app in "
                "front. It works for 5 minutes after you speak; nothing is kept after that.",
                "small",
                wrap=True,
            )
        )
        retype_row = QHBoxLayout()
        retype_row.setSpacing(8)
        self.retype_input = ShortcutCaptureInput()
        self.retype_input.setPlaceholderText("Off. Click to set one.")
        self.retype_off_button = button("Turn off")
        retype_row.addWidget(self.retype_input, 1)
        retype_row.addWidget(self.retype_off_button)
        retype_layout.addLayout(retype_row)
        self.body.addWidget(retype)

        self.body.addWidget(CubeTitle("App profiles"))
        profiles = QFrame()
        profiles.setObjectName("glass")
        self.profiles_layout = QVBoxLayout(profiles)
        self.profiles_layout.setContentsMargins(14, 10, 14, 12)
        self.profiles_layout.setSpacing(8)
        self.profiles_layout.addWidget(
            label(
                "Typing per app, picked by the app you dictate into. Type suits games that ignore Ctrl+V; "
                "Nothing as the send key keeps Enter from running a terminal command. Default follows "
                "the switches in the main window.",
                "small",
                wrap=True,
            )
        )
        self.profile_table: QWidget | None = None
        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.profile_name_combo = SafeComboBox()
        self.profile_name_combo.setEditable(True)
        self.profile_name_combo.lineEdit().setPlaceholderText("discord.exe")
        self.profile_name_combo.setToolTip("Apps you dictated into recently, or type a program name")
        self.add_profile_button = button("Add app")
        add_row.addWidget(self.profile_name_combo, 1)
        add_row.addWidget(self.add_profile_button)
        self.profiles_layout.addLayout(add_row)
        self.body.addWidget(profiles)

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
        self.tutorial_button = button("How it works")
        for index, widget in enumerate(
            (
                self.tutorial_button,
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

    def show_profiles(self, profiles: dict[str, dict[str, str]], recent_apps: list[str]) -> None:
        if self.profile_table is not None:
            self.profile_table.setParent(None)
            self.profile_table.deleteLater()
        table = QWidget()
        grid = QGridLayout(table)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        if profiles:
            for column, caption in enumerate(("App", "Insert", "Send key", "Full stop")):
                grid.addWidget(label(caption, "small"), 0, column)
        choices = (
            ("insert_method", INSERT_CHOICES, "Paste with Ctrl+V, or type key by key"),
            ("send_key", SEND_CHOICES, "The key pressed after the text"),
            ("full_stop", FULL_STOP_CHOICES, "End the text with a full stop"),
        )
        for row, (name, profile) in enumerate(sorted(profiles.items()), start=1):
            app = label(name)
            app.setToolTip(name)
            grid.addWidget(app, row, 0)
            for column, (field, options, tip) in enumerate(choices, start=1):
                combo = SafeComboBox()
                combo.setFixedWidth(92)
                combo.setToolTip(tip)
                for text, value in options:
                    combo.addItem(text, value)
                combo.setCurrentIndex(max(0, combo.findData(profile.get(field))))
                combo.currentIndexChanged.connect(
                    lambda index=0, app_name=name, key=field, box=combo: self._window.save_app_profile(
                        app_name, key, str(box.currentData())
                    )
                )
                grid.addWidget(combo, row, column)
            remove = button(GLYPHS["close"], "icon")
            remove.setToolTip(f"Remove the profile for {name}")
            remove.clicked.connect(lambda checked=False, app_name=name: self._window.remove_app_profile(app_name))
            grid.addWidget(remove, row, 4)
        grid.setColumnStretch(0, 1)
        self.profiles_layout.insertWidget(1, table)
        self.profile_table = table
        self.profile_name_combo.clear()
        self.profile_name_combo.addItems([app for app in recent_apps if app not in profiles])
        self.profile_name_combo.setEditText("")


class MainWindow(QMainWindow):
    model_changed = Signal(str)
    shortcut_changed = Signal(str, str)
    hotkey_enabled_changed = Signal(bool)
    feedback_volume_changed = Signal(float)
    feedback = Signal(bool)  # start or stop cue: the microphone opened or closed
    state_changed = Signal(str)
    problem = Signal(str, str)  # heading, what went wrong: for the tray, where the window usually is
    notice = Signal(str, str)  # heading, text: a tray notification that is no problem
    level_changed = Signal(float)
    transcription_done = Signal(int, str, float, float)
    transcription_failed = Signal(int, str)
    insertion_done = Signal(int, str, str)  # insert id; "done", "note", "cancelled" or "error"; message
    insert_timeout = Signal(int)
    cue = Signal(str)  # "cancel" or "error"
    rehook_requested = Signal()  # after unlock and resume Windows may have dropped the hooks
    escape_hold_changed = Signal(bool)  # Esc cancels instead of reaching the app while recording
    retype_shortcut_changed = Signal(str)
    unload_done = Signal()
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
        self._model_prepare_bytes = 0
        self._model_prepare_started_at = 0.0
        self._warm_up_generation = 0
        self._model_progress_timer = QTimer(self)
        self._model_progress_timer.setInterval(1000)
        self._model_progress_timer.timeout.connect(self._tick_model_progress)
        self._tutorial_pending = False
        self._warm_up_timer = QTimer(self)
        self._warm_up_timer.setSingleShot(True)
        self._warm_up_timer.setInterval(WARM_UP_DELAY_MS)
        self._warm_up_timer.timeout.connect(self.warm_up_selected_model)
        # Whether the transcription under way gets typed: Esc and a stuck
        # shortcut keep the text in Last transcription instead.
        self._deliver = True
        self._undelivered_reason = ""
        self._transcribing_model = ""
        self._start_cue_played = False
        self._pending_error = ""  # a mouse press that found no microphone: shown once it is a hold
        self._load_ahead = False  # this dictation loads Voz while it records
        self._insert_id = 0
        self._active_insert_id = 0
        self._insert_cancel: threading.Event | None = None
        self._insert_timer: threading.Timer | None = None
        self._retype_text = ""  # memory only, forgotten after RETYPE_KEEP_MS
        self._retype_timer = QTimer(self)
        self._retype_timer.setSingleShot(True)
        self._retype_timer.setInterval(RETYPE_KEEP_MS)
        self._retype_timer.timeout.connect(self._forget_retype)
        self._recent_apps: list[str] = []  # where dictations went this session, for profiles
        self._model_ready: dict[str, bool] = {}  # the list paints often; the Voz check stats F:
        # A mouse shortcut tapped within TAP_SECONDS stays a click, so its start
        # sound waits until it is a hold.
        self._cue_timer = QTimer(self)
        self._cue_timer.setSingleShot(True)
        self._cue_timer.setInterval(int(TAP_SECONDS * 1000))
        self._cue_timer.timeout.connect(self._play_delayed_start_cue)
        self._idle_unload_timer = QTimer(self)
        self._idle_unload_timer.setSingleShot(True)
        self._idle_unload_timer.timeout.connect(self._idle_unload)
        self._device_timer = QTimer(self)
        self._device_timer.setSingleShot(True)
        self._device_timer.setInterval(DEVICE_SETTLE_MS)
        self._device_timer.timeout.connect(self._devices_changed)

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
        try:  # unlock tells us to re-hook the shortcut (see nativeEvent)
            ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
                wintypes.HWND(int(self.winId())), NOTIFY_FOR_THIS_SESSION
            )
        except (AttributeError, OSError):
            pass
        self._connect_signals()
        self._load_settings_into_ui()
        self.set_status("Ready", self._ready_detail())
        self._refresh_model_status()
        # First-run setup waits for the window to be shown (showEvent): a hidden
        # sign-in start must not pop it up over a game.
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
        self.help_button = button("!", "caption")
        self.help_button.setToolTip("Would you like to replay the tutorial?")
        self.help_button.setAccessibleName("Replay the tutorial")
        self.help_button.setStyleSheet(
            'font-family: "Segoe UI Variable Display", "Segoe UI"; font-size: 16px; font-weight: 700; color: #EA8A2B;'
        )
        self.settings_button = button(GLYPHS["settings"], "caption")
        self.settings_button.setToolTip("Settings")
        self.minimize_button = button(GLYPHS["minimize"], "caption")
        self.maximize_button = button(GLYPHS["maximize"], "caption")
        self.close_button = button(GLYPHS["close"], "captionClose")
        for widget in (
            self.help_button,
            self.settings_button,
            self.minimize_button,
            self.maximize_button,
            self.close_button,
        ):
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
        self.model_status.setOpenExternalLinks(True)  # download errors carry a browser link
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
        self.feedback_volume_slider.setToolTip("Volume of the start, stop, cancel and error sounds. 0 turns them off.")
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
        self.last_transcript = _PrivateTextEdit()
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
        self.performance_note = label("Mostly for Whisper and FunASR.", "muted", wrap=True)
        layout.addWidget(self.performance_note)
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
        # One control instead of "Keep the model loaded" and "Low RAM mode",
        # which disagreed: Low RAM reloaded the model for every dictation.
        self.unload_combo = SafeComboBox()
        for text, value in UNLOAD_CHOICES:
            self.unload_combo.addItem(text, value)
        self.unload_combo.setToolTip(
            "Frees the model's memory (about 0.6 GB for Voz) when you stop dictating. "
            "The first dictation after that loads it again, which takes a couple of seconds."
        )
        grid.addWidget(label("CPU threads"), 0, 0)
        grid.addWidget(self.cpu_threads_combo, 0, 1)
        grid.addWidget(label("Whisper precision"), 1, 0)
        grid.addWidget(self.compute_combo, 1, 1)
        grid.addWidget(label("Free memory"), 2, 0)
        grid.addWidget(self.unload_combo, 2, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
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
        QMainWindow.showEvent(self, event)
        self.horizon.set_animated(True)
        # Setup and the tour show once, the first time the window is actually
        # opened: never on a tray-only start, so they cannot pop up over a game.
        if self._tutorial_pending:
            return
        if not bool(self.settings_manager.get("first_run_setup_completed", False)):
            self._tutorial_pending = True  # setup ends with the tour
            QTimer.singleShot(500, self.show_first_run_setup)
        elif not bool(self.settings_manager.get("tutorial_seen", False)):
            self._tutorial_pending = True
            QTimer.singleShot(700, self.show_tutorial)

    def show_tutorial(self, force: bool = False) -> None:
        if not force and bool(self.settings_manager.get("tutorial_seen", False)):
            return
        self.settings_manager.set("tutorial_seen", True)
        TutorialDialog(self).exec()

    def hideEvent(self, event) -> None:
        self.horizon.set_animated(False)
        super().hideEvent(event)

    def nativeEvent(self, event_type, message):
        """Unlock and resume can leave Windows without our shortcut hooks, and
        plugging a microphone in or out renumbers the device list."""
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if (msg.message == WM_WTSSESSION_CHANGE and msg.wParam == WTS_SESSION_UNLOCK) or (
                msg.message == WM_POWERBROADCAST and msg.wParam == PBT_APMRESUMEAUTOMATIC
            ):
                self.rehook_requested.emit()
            elif msg.message == WM_DEVICECHANGE and msg.wParam == DBT_DEVNODES_CHANGED:
                self.audio_recorder.mark_devices_changed()  # the next recording rescans first
                self._device_timer.start()
        return False, 0

    def _devices_changed(self) -> None:
        if not self._is_listening:  # never rescan under an open stream
            self.refresh_microphones()

    # ----------------------------------------------------------------- signals

    def _connect_signals(self) -> None:
        self.help_button.clicked.connect(lambda checked=False: self.show_tutorial(force=True))
        self.settings_button.clicked.connect(lambda checked=False: self.open_settings_window())
        self.minimize_button.clicked.connect(lambda checked=False: self.showMinimized())
        self.maximize_button.clicked.connect(lambda checked=False: self.toggle_maximized())
        self.close_button.clicked.connect(lambda checked=False: self.close())
        self.mic_button.clicked.connect(lambda checked=False: self.toggle_manual_listening())
        self.listen_button.clicked.connect(lambda checked=False: self.toggle_manual_listening())
        self.copy_transcript_button.clicked.connect(lambda checked=False: self.copy_transcript())
        self.clear_transcript_button.clicked.connect(lambda checked=False: self.clear_transcript())
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
        self.refresh_mics_button.clicked.connect(lambda checked=False: self.refresh_microphones(rescan=True))
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
        self.unload_combo.currentIndexChanged.connect(lambda index=0: self.save_unload_setting())
        for row in (
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
        settings.tutorial_button.clicked.connect(lambda checked=False: self.show_tutorial(force=True))
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
        settings.add_profile_button.clicked.connect(
            lambda checked=False: self.add_app_profile(settings.profile_name_combo.currentText())
        )
        settings.retype_input.capture_started.connect(self._start_retype_capture)
        settings.retype_input.shortcut_captured.connect(self.save_retype_shortcut)
        settings.retype_input.capture_cancelled.connect(self._end_retype_capture)
        settings.retype_off_button.clicked.connect(lambda checked=False: self.save_retype_shortcut(""))
        self.level_changed.connect(self.update_level)
        self.transcription_done.connect(self.finish_transcription)
        self.transcription_failed.connect(self.fail_transcription)
        self.insertion_done.connect(self.finish_insertion)
        self.insert_timeout.connect(self.handle_insert_timeout)
        self.unload_done.connect(self._refresh_model_status)
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
            self._show_startup_state()
            settings.keep_in_tray.switch.setChecked(bool(get("minimize_to_tray", True)))
            settings.debug_log.switch.setChecked(bool(get("debug_logs", False)))
            settings.retype_input.setText(str(get("retype_shortcut", "") or ""))
            self._restore_performance_controls()
        finally:
            self._loading_ui = False
        self.hotkey_enabled_changed.emit(True)  # the switch says on: so is the shortcut
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
            threads = get("cpu_threads", "auto")
            if self.cpu_threads_combo.findData(threads) < 0:  # a count v0.1 offered
                self.cpu_threads_combo.addItem(f"{threads} thread{'' if threads == 1 else 's'}", threads)
            self._select_combo_data(self.cpu_threads_combo, threads)
            self.fallback_cpu_row.switch.setChecked(bool(get("fallback_to_cpu", True)))
            self.low_vram_row.switch.setChecked(bool(get("low_vram_mode", False)))
            self._select_combo_data(self.unload_combo, str(get("unload_model_after", "never")))
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

    def refresh_microphones(self, rescan: bool = False) -> None:
        if rescan:
            self.audio_recorder.rescan()
        current = self._resolve_saved_microphone() or "missing"
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        for device in self.audio_recorder.list_input_devices(keep=current):
            self.mic_combo.addItem(device.name, device.id)
        index = self.mic_combo.findData(current)
        if index < 0 and current != "default":
            # The saved choice stays, so a headset that connects after logon
            # is used again once it is back.
            self._append_activity_log("saved microphone missing; using the default")
            self.set_status(
                "Microphone changed",
                "The saved microphone was not found, so the default system microphone is used until it is back.",
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
        self._model_ready = {model.name: self.model_manager.is_model_ready(model.name) for model in self.model_manager.models()}
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
            if self.settings_manager.model_folder_error:  # a drive that is not there
                self.model_status.setText(self.settings_manager.model_folder_error)
        self._show_what_applies()
        self._sync_model_list()
        self.update_bottom_labels()

    def _show_what_applies(self) -> None:
        """Voz always runs on the CPU, and precision is Whisper's alone: with
        another model selected, those controls changed nothing."""
        engine = self.model_manager.selected_info().engine
        self.device_segmented.setEnabled(engine != "voz")
        self.compute_combo.setEnabled(engine == "whisper")
        self.performance_note.setText(
            "Voz always runs on the CPU. Hardware and precision are for the other models."
            if engine == "voz"
            else "Mostly for Whisper and FunASR."
        )

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
        self._model_prepare_bytes = _folder_bytes(self.model_manager.model_storage_path())
        timer = threading.Timer(
            MODEL_PREPARE_STALL_SECONDS, lambda: self.model_prepare_timeout.emit(request_id)
        )
        timer.daemon = True
        self._model_prepare_timer = timer
        timer.start()

    def _reset_transcriber(self) -> None:
        self._warm_up_generation += 1
        old = self.transcriber
        self.transcriber = Transcriber(self.model_manager)
        # Off this thread: unload waits for a dictation still using the old model.
        threading.Thread(target=old.unload, daemon=True).start()

    def _schedule_warm_up(self) -> None:
        self._warm_up_timer.start()

    def _arm_idle_unload(self) -> None:
        """Restarts the countdown to freeing the model. "never" has none, and
        "each" frees it inside the transcriber after every dictation."""
        value = str(self.settings_manager.get("unload_model_after", "never"))
        if value.isdigit():
            self._idle_unload_timer.start(int(value) * 60 * 1000)
        else:
            self._idle_unload_timer.stop()

    def _idle_unload(self) -> None:
        if self._pipeline_busy():
            self._arm_idle_unload()  # try again after the next idle stretch
            return
        transcriber = self.transcriber
        if not transcriber.is_loaded():
            return
        self._append_activity_log("idle: freeing the model")
        threading.Thread(target=self._unload_worker, args=(transcriber,), daemon=True).start()

    def _unload_worker(self, transcriber: Transcriber) -> None:
        try:
            transcriber.unload()  # freeing it holds the GIL ~80 ms
        except Exception as exc:
            self._append_activity_log(f"unload failed: {_error_text(exc)}")
        self.unload_done.emit()

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

    def warm_up_at_once(self) -> None:
        """For a start in the tray: the usual wait keeps an opening window
        smooth, and there is none."""
        self._warm_up_timer.stop()
        self.warm_up_selected_model()

    def warm_up_selected_model(self, while_listening: bool = False) -> None:
        """Load the installed model in the background so the first dictation is instant.

        Recording stays available meanwhile; a dictation that finishes first
        simply waits for the load inside the transcriber. `while_listening`
        is the load a dictation started: it runs while that one records.
        """
        settings = self.settings_manager.all()
        busy = (self._is_transcribing or self._is_preparing_model) if while_listening else self._pipeline_busy()
        if (
            busy
            or self.transcriber.is_loaded()
            or not self.model_manager.is_model_ready()
            or str(settings.get("unload_model_after", "never")) == "each"
        ):
            return
        self._warm_up_generation += 1
        generation = self._warm_up_generation
        transcriber = self.transcriber
        model_name = self.model_manager.selected_model()
        self._append_activity_log(f"warm-up start: {model_name}{' while listening' if while_listening else ''}")
        self.model_status.setText(f"Loading {self.model_manager.selected_info().label} into memory...")
        threading.Thread(
            target=self._warm_up_worker,
            args=(generation, transcriber, settings, model_name, while_listening),
            daemon=True,
        ).start()

    def _warm_up_worker(
        self,
        generation: int,
        transcriber: Transcriber,
        settings: dict[str, Any],
        model_name: str,
        while_listening: bool = False,
    ) -> None:
        if while_listening:
            # The recorder dates its audio by the first buffers, so they come
            # in before the load's GIL hold can stall them.
            self.audio_recorder.wait_for_audio(1.0)
        started = time.perf_counter()
        try:
            transcriber.prepare_selected_model(settings, model_name=model_name)
        except Exception as exc:
            self.warm_up_done.emit(generation, _error_text(exc))
        else:
            self._append_activity_log(f"warm-up done in {time.perf_counter() - started:.1f}s")
            self.warm_up_done.emit(generation, "")

    def finish_warm_up(self, generation: int, error: str) -> None:
        if generation != self._warm_up_generation:
            return
        if error:
            self._append_activity_log(f"warm-up failed: {error}")
        self._arm_idle_unload()  # a model nobody dictates with is freed too
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
        if self._pipeline_busy():  # a dictation started while the question was open
            self._sync_model_list()
            self.set_status("Busy", self._pipeline_busy_message())
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
        # Only a click asks here: select_model already asked when the model was
        # picked, and a shortcut press must never open a dialog over a game.
        if (
            not auto
            and self.model_manager.is_large_model()
            and bool(self.settings_manager.get("warn_before_large_models", True))
        ):
            if not self._confirm_model_choice(selected):
                return
        if self._is_listening or self._is_transcribing or selected != self.model_manager.selected_model():
            return  # things moved on while a question was open

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
            args=(request_id, settings, transcriber, selected),
            daemon=True,
        ).start()

    def _prepare_model_worker(
        self, request_id: int, settings: dict[str, Any], transcriber: Transcriber, model_name: str
    ) -> None:
        # The name is pinned: a model picked meanwhile was marked installed instead.
        try:
            transcriber.prepare_selected_model(settings, model_name=model_name)
        except Exception as exc:
            self.model_failed.emit(request_id, _error_text(exc))
        else:
            self.model_ready.emit(request_id, model_name)

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
        self._arm_idle_unload()
        self._refresh_model_status()
        self.set_status("Ready", self.transcriber.runtime_message or self._ready_detail())

    def fail_model_prepare(self, request_id: int, message: str) -> None:
        if request_id != self._active_model_prepare_id:
            return
        self._end_model_prepare()
        self._reset_transcriber()
        self._append_activity_log(f"model prepare failed: {message}")
        self._refresh_model_status()
        self.model_status.setText(_linkify(message))
        self.set_status("Error", message)

    def handle_model_prepare_timeout(self, request_id: int) -> None:
        if request_id != self._active_model_prepare_id or not self._is_preparing_model:
            return
        if _folder_bytes(self.model_manager.model_storage_path()) != self._model_prepare_bytes:
            self._start_model_prepare_timer(request_id)  # slow but still arriving
            return
        self._end_model_prepare()
        self._reset_transcriber()
        message = "Model preparation stalled: nothing new arrived for 5 minutes. Check your connection, or pick a smaller model."
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
                if have < 1_000_000:  # nothing much yet: say it is connecting, not "0 MB"
                    detail = f"Connecting to huggingface.co to download {info.label} ({elapsed}s)."
                else:
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

    def shortcut_pressed(self) -> None:
        """Hold mode starts here. Toggle mode starts or stops by this window's
        own state, so a refused start or a stop from the tray leaves nothing
        out of step."""
        if self._is_listening and str(self.settings_manager.get("shortcut_mode", "hold")) == "toggle":
            self.stop_listening_and_transcribe()
        else:
            self.start_listening(from_shortcut=True)

    def _append_activity_log(self, message: str) -> None:
        app_log.log(message)

    def _set_listening(self, listening: bool) -> None:
        self._is_listening = listening
        self._start_cue_played = False  # the stop sound only follows a start sound
        self.escape_hold_changed.emit(listening)  # Esc cancels instead of opening a game menu
        if not listening:
            self._cue_timer.stop()

    def _play_delayed_start_cue(self) -> None:
        """TAP_SECONDS after a mouse shortcut went down: it is a hold, not a click."""
        if self._is_listening:
            self._start_cue_played = True
            self.feedback.emit(True)
            if self._load_ahead:
                self.warm_up_selected_model(while_listening=True)
        elif self._pending_error:  # the microphone did not open
            message, self._pending_error = self._pending_error, ""
            self.set_status("Error", message)
            self.cue.emit("error")

    def _mouse_hold_shortcut(self) -> bool:
        if str(self.settings_manager.get("shortcut_mode", "hold")) != "hold":
            return False
        try:
            return parse_shortcut(str(self.settings_manager.get("shortcut", ""))).mouse_trigger is not None
        except ValueError:
            return False

    def _dictation_toggled(self, enabled: bool) -> None:
        if self._loading_ui:
            return
        if not enabled and self._is_listening:
            self.cancel_listening()  # the shortcut that would end it is about to stop
        if not enabled and self._active_transcription_id and self._deliver:
            self._deliver = False  # nothing more gets typed: shown, not typed
            self._undelivered_reason = "paused"
        if not enabled and self._insert_cancel is not None:
            self._insert_cancel.set()
        self.hotkey_enabled_changed.emit(enabled)
        self.set_status(
            "Ready" if enabled else "Paused",
            self._ready_detail() if enabled else "The shortcut is paused. Turn Dictation on to use it again.",
        )

    def start_listening(self, from_shortcut: bool = False) -> None:
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
        # None for this app's own windows and the taskbar: nothing to type into.
        self._target_hwnd = self.text_inserter.get_foreground_window()
        # Voz freed for idle time loads while you speak, not after the
        # release. Its load holds the GIL for 0.7 s, which the recording's
        # headroom rides out. ponytail: Voz only; measure Whisper's and
        # FunASR's GIL holds before they load ahead too.
        load_ahead = (
            self.model_manager.model_engine() == "voz"
            and not self.transcriber.is_loaded()
            and str(self.settings_manager.get("unload_model_after", "never")) != "each"
        )
        try:
            self.audio_recorder.start(str(microphone_id), self.level_changed.emit, headroom=load_ahead)
        except AudioRecorderError:
            # Unplugged since the device list was read? Read it again, once.
            self.audio_recorder.rescan()
            try:
                self.audio_recorder.start(
                    self._resolve_saved_microphone() or "default", self.level_changed.emit, headroom=load_ahead
                )
            except AudioRecorderError as exc:
                self._append_activity_log("microphone start failed")
                if from_shortcut and self._mouse_hold_shortcut():
                    # A tap stays a click: only a hold sees and hears the error.
                    # Shown at once, a plain click raised a tray notification.
                    self._pending_error = _error_text(exc)
                    self._cue_timer.start()
                else:
                    self.set_status("Error", _error_text(exc))
                    self.cue.emit("error")
                return
        self._append_activity_log("listening")
        self._set_listening(True)
        self._load_ahead = load_ahead
        mouse_hold = from_shortcut and self._mouse_hold_shortcut()
        if mouse_hold:
            self._cue_timer.start()  # a tap stays a click: no sound, and no load
        else:
            self._start_cue_played = True
            self.feedback.emit(True)
        mode = str(self.settings_manager.get("shortcut_mode", "hold"))
        self.set_status(
            "Listening",
            "Speak now. Release the shortcut to type it."
            if mode == "hold"
            else "Speak now. Press the shortcut again to type it.",
        )
        if load_ahead and not mouse_hold:  # last: the start sound and Esc hold-back never wait for it
            self.warm_up_selected_model(while_listening=True)

    def _selected_microphone_id(self) -> str:
        microphone_id = self._resolve_saved_microphone()
        if not microphone_id:  # connected since the device list was read?
            self.audio_recorder.rescan()
            microphone_id = self._resolve_saved_microphone()
        if microphone_id:
            return microphone_id
        self._append_activity_log("microphone missing before recording; using default")
        self.refresh_microphones()
        return "default"

    def stop_listening_and_transcribe(self, release_window_hwnd: object = None, deliver: bool = True) -> None:
        if not self._is_listening:
            return
        start_cue_played = self._start_cue_played
        self._set_listening(False)
        if start_cue_played:  # a hold let go before its start sound gets no stop sound
            self.feedback.emit(False)
        try:
            release_window = int(release_window_hwnd) if release_window_hwnd is not None else None
        except (TypeError, ValueError):
            release_window = None
        if release_window:
            # Title and app as they were at release: a chat switch while it
            # transcribes is caught by comparing them (no Enter then).
            captured_target = self.text_inserter.get_window_target(release_window)
            if captured_target is not None:
                self._target_hwnd = captured_target
        try:
            audio = self.audio_recorder.stop()
        except AudioRecorderError as exc:
            self._append_activity_log("microphone stop failed")
            self.cue.emit("error")
            self.set_status("Error", _error_text(exc))
            return
        if audio.size < TOO_SHORT_SECONDS * SAMPLE_RATE:
            self._target_hwnd = None
            self._append_activity_log("recording too short")
            self.set_status("Ready", "Too short to type. Hold the shortcut while you speak.")
            return

        self._is_transcribing = True
        self._deliver = deliver
        self._undelivered_reason = "" if deliver else "timeout"
        self._idle_unload_timer.stop()
        self._transcription_request_id += 1
        request_id = self._transcription_request_id
        self._active_transcription_id = request_id
        self._transcribing_model = self.model_manager.selected_model()  # a pick meanwhile waits
        self._append_activity_log(f"captured {audio.size / SAMPLE_RATE:.1f}s of audio")
        self.set_status("Transcribing", "Turning your speech into text on this PC.")
        settings = self.settings_manager.all()
        self._start_transcription_timer(request_id, audio.size / SAMPLE_RATE)
        threading.Thread(
            target=self._transcribe_worker,
            args=(request_id, audio, settings, self.transcriber, self._transcribing_model),
            daemon=True,
        ).start()

    def stop_after_timeout(self) -> None:
        """The shortcut's watchdog gave up waiting for the release (a missed
        key-up, or a 5-minute hold): keep what was said, type nothing."""
        if self._is_listening:
            self.stop_listening_and_transcribe(deliver=False)

    def drop_tapped_recording(self) -> None:
        """A short tap of a mouse shortcut stays a normal click: the recording
        it opened goes away without a sound."""
        self._cue_timer.stop()
        self._pending_error = ""
        if not self._is_listening:
            return
        self._set_listening(False)
        self.audio_recorder.cancel()
        self._target_hwnd = None
        self._append_activity_log("shortcut tapped: passed on as a click")
        self.set_status("Ready", self._ready_detail())

    def cancel_listening(self) -> None:
        """Esc: drops a recording, or keeps a result from being typed."""
        if self._is_listening:
            self._set_listening(False)
            self.audio_recorder.cancel()
            self.cue.emit("cancel")
            self._append_activity_log("recording cancelled")
            self.set_status("Ready", "Recording cancelled.")
        elif self._active_transcription_id and self._deliver:
            self._deliver = False
            self._undelivered_reason = "cancelled"
            self.cue.emit("cancel")
            self._append_activity_log("typing cancelled before the text was ready")
            self.set_status("Transcribing", "Cancelled. The text will show in Last transcription but won't be typed.")
        elif self._active_insert_id and self._insert_cancel is not None:
            self._insert_cancel.set()  # stops before the next key it would send
            self.cue.emit("cancel")
            self._append_activity_log("typing cancelled")

    def _transcribe_worker(
        self,
        request_id: int,
        audio: np.ndarray,
        settings: dict[str, Any],
        transcriber: Transcriber,
        model_name: str,
    ) -> None:
        seconds = audio.size / SAMPLE_RATE
        started = time.perf_counter()
        try:
            text = transcriber.transcribe(audio, settings, model_name=model_name)
        except Exception as exc:
            self._append_activity_log(f"transcription failed: id={request_id} {exc.__class__.__name__}")
            self.transcription_failed.emit(request_id, _error_text(exc))
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
        target, self._target_hwnd = self._target_hwnd, None
        self._show_last_dictation(text, audio_seconds, elapsed)
        self._arm_idle_unload()
        if not text:
            self.set_status(
                "Ready",
                self.transcriber.runtime_message
                or "No speech was detected. Try speaking closer to the microphone.",
            )
            return
        self.last_transcript.setPlainText(text)
        self._remember_for_retype(text)
        if not self._deliver:
            if self._undelivered_reason == "timeout":
                self.cue.emit("error")
                self.set_status(
                    "Not typed",
                    "The shortcut seemed stuck, so the recording was stopped. The text is in Last "
                    "transcription and was not typed.",
                )
            elif self._undelivered_reason == "paused":
                self.set_status("Paused", "Dictation is off, so the text is in Last transcription and was not typed.")
            else:
                self.set_status("Ready", "Cancelled. The text is in Last transcription and was not typed.")
            return
        if target is None:
            self.set_status(
                "Ready", "No app was in front to type into, so the text is in Last transcription."
            )
            return
        self._start_insert(text, target)

    def _start_insert(self, text: str, target: TextTarget) -> None:
        # Busy first, then the status: the scene showed idle while it typed.
        # Typing can take seconds in games, so it runs off the UI thread, and a
        # new recording cannot start in the middle of it.
        self._is_transcribing = True
        self.set_status("Typing", "Typing into the app you were using.")
        self._remember_app(target)
        self._insert_id += 1
        insert_id = self._active_insert_id = self._insert_id
        cancel = self._insert_cancel = threading.Event()
        # A hung insert is called off with its token, so a late wake-up types
        # nothing into whatever is in front by then.
        timer = threading.Timer(
            INSERT_TIMEOUT_SECONDS + INSERT_SECONDS_PER_CHAR * len(text),
            lambda: self.insert_timeout.emit(insert_id),
        )
        timer.daemon = True
        self._insert_timer = timer
        timer.start()
        threading.Thread(
            target=self._insert_worker,
            args=(insert_id, text, target, self._settings_for(target), cancel),
            daemon=True,
        ).start()

    def _settings_for(self, target: TextTarget | None) -> dict[str, Any]:
        """The typing settings with the target app's profile applied."""
        settings = self.settings_manager.all()
        profiles = settings.get("app_profiles") or {}
        profile = profiles.get(getattr(target, "process_name", "") or "") if isinstance(profiles, dict) else None
        if not isinstance(profile, dict):
            return settings
        if profile.get("insert_method", "auto") != "auto":
            settings["insert_method"] = profile["insert_method"]
        if profile.get("send_key", "default") != "default":
            settings["send_key"] = profile["send_key"]
        if profile.get("full_stop", "default") != "default":
            settings["auto_punctuation"] = profile["full_stop"] == "on"
        return settings

    def _insert_worker(
        self,
        insert_id: int,
        text: str,
        target: TextTarget,
        settings: dict[str, Any],
        cancel: threading.Event,
    ) -> None:
        try:
            note = self.text_inserter.insert_text(text, target, settings, cancel=cancel)
        except InsertionCancelled as exc:
            self.insertion_done.emit(insert_id, "cancelled", _error_text(exc))
        except TextInsertionError as exc:
            self.insertion_done.emit(insert_id, "error", _error_text(exc))
        except Exception as exc:
            self.insertion_done.emit(insert_id, "error", f"Could not type the text: {_error_text(exc)}")
        else:
            self.insertion_done.emit(insert_id, "note" if note else "done", note or "")

    def finish_insertion(self, insert_id: int, kind: str, message: str) -> None:
        if insert_id != self._active_insert_id:
            return  # the watchdog already gave up on it
        self._active_insert_id = 0
        self._insert_cancel = None
        if self._insert_timer is not None:
            self._insert_timer.cancel()
            self._insert_timer = None
        self._is_transcribing = False
        self._arm_idle_unload()
        if kind == "error":
            self._append_activity_log("text insertion failed")
            self.cue.emit("error")
            self.set_status("Error", f"{message} {self._retype_hint()}".strip())
        elif kind == "cancelled":
            self._append_activity_log("text insertion cancelled")
            self.set_status("Ready", message)
        elif kind == "note":
            self._append_activity_log("text inserted, send key skipped")
            self.set_status("Not sent", message)
        else:
            self._append_activity_log("text inserted")
            self.set_status("Ready", self.transcriber.runtime_message or self._ready_detail(done=True))

    def handle_insert_timeout(self, insert_id: int) -> None:
        if insert_id != self._active_insert_id:
            return
        if self._insert_cancel is not None:
            self._insert_cancel.set()
        self._insert_cancel = None
        self._insert_timer = None
        self._active_insert_id = 0
        self._is_transcribing = False
        self._append_activity_log("text insertion timed out")
        self.cue.emit("error")
        self.set_status(
            "Error", f"Typing took too long and was stopped. The text is in Last transcription. {self._retype_hint()}".strip()
        )

    def _retype_hint(self) -> str:
        shortcut = str(self.settings_manager.get("retype_shortcut", "") or "")
        return f"Press {shortcut} to type it again." if shortcut else ""

    def _remember_for_retype(self, text: str) -> None:
        self._retype_text = text
        self._retype_timer.start()

    def _forget_retype(self) -> None:
        self._retype_text = ""

    def _remember_app(self, target: TextTarget) -> None:
        name = str(getattr(target, "process_name", "") or "").lower()
        if not name:
            return
        self._recent_apps = [name] + [app for app in self._recent_apps if app != name][:7]

    def retype_last_dictation(self) -> None:
        """The retype shortcut: the last dictation again, into the app in front."""
        if not self._retype_text:
            self.set_status("Ready", "There is no recent dictation to type again.")
            return
        if self._pipeline_busy():
            self.set_status("Busy", self._pipeline_busy_message())
            return
        target = self.text_inserter.get_foreground_window()
        if target is None:
            self.set_status("Ready", "Click into the app to type into, then press the shortcut again.")
            return
        self._start_insert(self._retype_text, target)

    def _show_last_dictation(self, text: str, audio_seconds: float, elapsed: float) -> None:
        info = MODEL_INFO.get(self._transcribing_model) or self.model_manager.selected_info()
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
        self._arm_idle_unload()
        self.cue.emit("error")
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
        self.cue.emit("error")
        self.set_status(
            "Error",
            "Transcription took too long, so the speech engine was reset. Try again, or pick a faster model.",
        )

    def clear_transcript(self) -> None:
        self.last_transcript.clear()
        self._forget_retype()  # cleared means gone: the retype shortcut types nothing either

    def copy_transcript(self) -> None:
        text = self.last_transcript.toPlainText()
        if not text:
            return
        try:
            # Marked private, like a paste: no Win+V history, cloud clipboard or monitors.
            self.text_inserter.set_clipboard_text(text, private=True)
        except TextInsertionError as exc:
            self.set_status("Error", str(exc))
            return
        self.set_status("Ready", "The last transcription was copied to the clipboard.")

    # --------------------------------------------------------------- settings

    def _shortcut_accepted(self, shortcut: str, saved: str) -> bool:
        """Refuses shortcuts that cannot work (Esc cancels itself, a bare Enter
        fires while typing) and asks before saving risky ones."""
        try:
            parse_shortcut(shortcut)
            problem = shortcut_problem(shortcut)
        except ValueError as exc:
            problem = str(exc)
        retype = str(self.settings_manager.get("retype_shortcut", "") or "")
        if not problem and retype and same_shortcut(shortcut, retype):  # one press would do both
            problem = f"{retype} types the last dictation again. Pick another shortcut, or change that one in Settings."
        if problem:
            show_message(self, "Shortcut problem", problem)
            return False
        warning = shortcut_warning(shortcut)
        if warning and shortcut != saved:
            return show_message(
                self, "Shortcut warning", f"{warning} Save this shortcut anyway?", ("Save it", "Cancel")
            )
        return True

    def apply_shortcut_settings(self) -> None:
        shortcut = self.shortcut_input.text().strip() or "Ctrl+Alt+Q"
        saved = str(self.settings_manager.get("shortcut", ""))
        if not self._shortcut_accepted(shortcut, saved):
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
        self.update_shortcut_warning()
        self.set_status("Shortcut capture", "Press the shortcut you want to use. Press Esc to cancel.")

    def finish_shortcut_capture(self, shortcut: str) -> None:
        self._is_capturing_shortcut = False
        self.shortcut_input.setText(shortcut)
        self.apply_shortcut_settings()
        self.update_shortcut_warning()  # the box's own text changes came while capturing

    def cancel_shortcut_capture(self) -> None:
        self._is_capturing_shortcut = False
        self.update_shortcut_warning()
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
                "restore_clipboard": self.restore_clipboard_row.switch.isChecked(),
                "add_space_after_text": self.add_space_row.switch.isChecked(),
                "press_enter_after_text": self.press_enter_row.switch.isChecked(),
                "auto_capitalization": self.capitalize_row.switch.isChecked(),
                "auto_punctuation": self.period_row.switch.isChecked(),
            }
        )

    def add_app_profile(self, name: str) -> None:
        name = str(name or "").strip().lower()
        if len(name) <= 4 or not name.endswith(".exe") or any(char in name for char in '\\/:*?"<>|'):
            self.set_status("Ready", "Type the app's program name, for example discord.exe.")
            return
        profiles = _copy_profiles(self.settings_manager)
        profiles.setdefault(name, dict(NEW_PROFILE))
        self.settings_manager.set("app_profiles", profiles)
        self.settings_window.show_profiles(profiles, self._recent_apps)

    def save_app_profile(self, name: str, field: str, value: str) -> None:
        profiles = _copy_profiles(self.settings_manager)
        if name in profiles:
            profiles[name][field] = value
            self.settings_manager.set("app_profiles", profiles)

    def remove_app_profile(self, name: str) -> None:
        profiles = _copy_profiles(self.settings_manager)
        profiles.pop(name, None)
        self.settings_manager.set("app_profiles", profiles)
        self.settings_window.show_profiles(profiles, self._recent_apps)

    def _start_retype_capture(self) -> None:
        self._is_capturing_shortcut = True  # its keys must not start a dictation

    def _end_retype_capture(self) -> None:
        self._is_capturing_shortcut = False
        self.settings_window.retype_input.setText(str(self.settings_manager.get("retype_shortcut", "") or ""))

    def save_retype_shortcut(self, shortcut: str) -> None:
        self._is_capturing_shortcut = False
        shortcut = str(shortcut or "").strip()
        problem = ""
        if shortcut:
            try:
                if parse_shortcut(shortcut).mouse_trigger:
                    problem = "Use a key combination here: the mouse is only watched for the dictation button."
                else:
                    problem = shortcut_problem(shortcut, retype=True)
            except ValueError as exc:
                problem = str(exc)
            if not problem and same_shortcut(shortcut, str(self.settings_manager.get("shortcut", ""))):
                problem = "That is already the dictation shortcut."
        if problem:
            show_message(self, "Shortcut problem", problem)
            self._end_retype_capture()
            return
        self.settings_manager.set("retype_shortcut", shortcut)
        self.settings_window.retype_input.setText(shortcut)
        self.retype_shortcut_changed.emit(shortcut)
        self.set_status(
            "Ready",
            f"{shortcut} types the last dictation again." if shortcut else "The retype shortcut is off.",
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
            self._show_startup_state()
            self.set_status("Error", str(exc))
            return
        self.settings_manager.set("start_with_windows", enabled)
        self._show_startup_state()
        self.set_status(
            "Ready",
            "Whisper Anywhere will start hidden in the tray when Windows starts."
            if enabled
            else "Whisper Anywhere will not start with Windows.",
        )

    def _show_startup_state(self) -> None:
        """The switch shows what Windows will really do: Task Manager can turn
        the entry off without telling the app, and another copy can own it."""
        try:
            state = self.startup_manager.state()
        except StartupManagerError:
            state = "off"
        settings = self.settings_window
        switch = settings.start_with_windows.switch
        was_blocked = switch.blockSignals(True)
        switch.setChecked(state == "on")
        switch.blockSignals(was_blocked)
        note = {
            "disabled": "Turned off in Task Manager's Startup apps. Switch it on here to turn it back on.",
            "other": "Another copy of Whisper Anywhere starts with Windows. Switch it on to start this copy instead.",
        }.get(state, "")
        settings.startup_note.setText(note)
        settings.startup_note.setVisible(bool(note))

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
                "device": device,
                "use_gpu_if_available": device != "cpu",
                "compute_type": str(self.compute_combo.currentData() or "auto"),
                "cpu_threads": self.cpu_threads_combo.currentData() or "auto",
                "low_vram_mode": self.low_vram_row.switch.isChecked(),
                "fallback_to_cpu": self.fallback_cpu_row.switch.isChecked(),
                "warn_before_large_models": self.warn_large_row.switch.isChecked(),
                "auto_download_models": self.auto_download_row.switch.isChecked(),
            }
        )
        self._reset_transcriber()
        self._refresh_model_status()
        self._schedule_warm_up()
        self.set_status("Ready", "Performance settings saved.")

    def save_unload_setting(self) -> None:
        """Takes effect without a reload: only when the model is freed changes."""
        if self._loading_ui:
            return
        self.settings_manager.set("unload_model_after", str(self.unload_combo.currentData() or "never"))
        self._arm_idle_unload()
        if self.settings_manager.get("unload_model_after") != "each":
            self._schedule_warm_up()
        self.set_status("Ready", f"Free memory: {self.unload_combo.currentText().lower()}.")

    def show_first_run_setup(self, force: bool = False) -> None:
        if not force and bool(self.settings_manager.get("first_run_setup_completed", False)):
            return
        dialog = FirstRunSetupDialog(self)
        # Its shortcut box captures like the main window's: the shortcut must not
        # start a dictation that pastes, and presses Enter, into the box.
        box = dialog.shortcut_input
        box.capture_started.connect(lambda: setattr(self, "_is_capturing_shortcut", True))
        box.shortcut_captured.connect(lambda shortcut: setattr(self, "_is_capturing_shortcut", False))
        box.capture_cancelled.connect(lambda: setattr(self, "_is_capturing_shortcut", False))
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._is_capturing_shortcut = False
        if not accepted:
            if not force:
                self.settings_manager.set("first_run_setup_completed", True)
            self.set_status("Ready", "Setup skipped. Current settings are unchanged.")
            self.show_tutorial()
            return
        shortcut = dialog.shortcut_input.text().strip() or "Ctrl+Alt+Q"
        saved = str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q"))
        if not self._shortcut_accepted(shortcut, saved):
            shortcut = saved  # the rest of the setup still counts
        device = str(dialog.device_combo.currentData() or "cpu")
        microphone = str(dialog.mic_combo.currentData() or "default")
        values = {
            "shortcut": shortcut,
            "selected_model": str(dialog.model_combo.currentData() or "voz"),
            # The name too: the old microphone's saved name took the choice back.
            "microphone_device": microphone,
            "microphone_name": "" if microphone == "default" else dialog.mic_combo.currentText(),
            "device": device,
            "use_gpu_if_available": device != "cpu",
            "first_run_setup_completed": True,
        }
        if device != self.settings_manager.get("device"):
            values["compute_type"] = "auto"  # precision follows the new hardware
        self.settings_manager.update(values)
        self.shortcut_changed.emit(shortcut, str(self.settings_manager.get("shortcut_mode", "hold")))
        self.model_changed.emit(self.model_manager.selected_model())
        self._reset_transcriber()
        self._load_settings_into_ui()
        self._refresh_model_status()
        self.prepare_selected_model(auto=True)
        self._schedule_warm_up()
        self.set_status("Ready", f"Setup saved. {self._ready_detail()}")
        self.show_tutorial()  # the model downloads in the background meanwhile

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
            "recent_logs": app_log.recent(40),
        }
        # The user name hides in paths, device names and log lines alike.
        return app_log.scrub_user_paths(json.dumps(payload, indent=2))

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
            values = json.loads(Path(path).read_bytes())  # bytes: a BOM or UTF-16 is fine too
            if isinstance(values, dict) and "shortcut" in values:
                parse_shortcut(str(values["shortcut"]))  # a bad one stopped every later launch
                if problem := shortcut_problem(str(values["shortcut"])):
                    raise ValueError(problem)
            if isinstance(values, dict) and str(values.get("retype_shortcut") or ""):
                retype = str(values["retype_shortcut"])
                dictation = str(values.get("shortcut") or self.settings_manager.get("shortcut", ""))
                unusable = parse_shortcut(retype).mouse_trigger or shortcut_problem(retype, retype=True)
                if unusable or same_shortcut(retype, dictation):
                    raise ValueError(f"the retype shortcut {retype} can't be used")
            left_out: list[str] = []
            if isinstance(values, dict) and "selected_model" in values:
                # A model whose engine this copy lacks (SenseVoice from a build
                # with FunASR): the list here does not show it, so it stays out.
                model = self.model_manager.normalize_model_name(str(values["selected_model"]))
                if model in MODEL_INFO and not engine_installed(MODEL_INFO[model].engine):
                    values = {key: value for key, value in values.items() if key != "selected_model"}
                    left_out.append("selected_model (its speech engine is not installed here)")
            ignored = left_out + self.settings_manager.import_portable(values)
        except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
            show_message(self, "Settings import failed", f"Could not import settings: {exc}")
            return
        self._load_settings_into_ui()
        # The switch was set while loading, which skips its handler.
        debug = bool(self.settings_manager.get("debug_logs", False))
        app_log.write_to_disk(self.settings_manager.debug_log_path if debug else None)
        self._reset_transcriber()
        self._refresh_model_status()
        self.shortcut_changed.emit(
            str(self.settings_manager.get("shortcut", "Ctrl+Alt+Q")),
            str(self.settings_manager.get("shortcut_mode", "hold")),
        )
        self.model_changed.emit(self.model_manager.selected_model())
        self.feedback_volume_changed.emit(float(self.settings_manager.get("feedback_volume", 0.5)))
        self.retype_shortcut_changed.emit(str(self.settings_manager.get("retype_shortcut", "") or ""))
        self._arm_idle_unload()
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
        self._show_startup_state()  # Task Manager may have changed it meanwhile
        window.show_profiles(_copy_profiles(self.settings_manager), self._recent_apps)
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        window.setMaximumHeight(screen.height())  # taller than the screen, it scrolls
        window.adjustSize()
        frame = self.frameGeometry()
        # Centred on the main window, but never past the screen's edges: it
        # opened cut off below a short main window.
        x = frame.center().x() - window.width() // 2
        y = frame.top() + max(0, (frame.height() - window.height()) // 2)
        window.move(
            max(screen.left(), min(x, screen.right() + 1 - window.width())),
            max(screen.top(), min(y, screen.bottom() + 1 - window.height())),
        )
        window.show()
        window.raise_()
        window.activateWindow()

    # ------------------------------------------------------------------ status

    def update_shortcut_warning(self) -> None:
        text = self.shortcut_input.text().strip() or "Ctrl+Alt+Q"
        try:
            parse_shortcut(text)
        except ValueError:  # still being captured: no parse error under it
            self.shortcut_warning.setText("")
            return
        self.shortcut_warning.setText("" if self._is_capturing_shortcut else shortcut_warning(text))

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
        if self._status_is_error:
            # Before the state: the tray's red dot then already knows why. A
            # plain error has no heading of its own.
            self.problem.emit("" if status == "Error" else status, detail)
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
        if state != self._last_state:  # "error" too: the tray showed it as idle
            self._last_state = state
            self.state_changed.emit(state)

    def set_hotkey_status(self, message: str) -> None:
        self.hotkey_status_label.setText(message)

    def show_hotkey_error(self, message: str) -> None:
        self.set_hotkey_status(message)
        self.set_status("Hotkey error", message)
        if self.isActiveWindow():  # only while it is in use: never over a game (minimized counts as visible)
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
        if QApplication.screenAt(self.frameGeometry().center()) is None:
            self._fit_to_screen()  # its monitor was unplugged
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
            if not self.settings_manager.get("tray_hint_shown", False):
                # Once: the window just vanished, and nothing said the app goes on.
                self.settings_manager.set("tray_hint_shown", True)
                self.notice.emit(
                    "Whisper Anywhere is still running",
                    "The shortcut keeps working. To quit, right-click this icon and choose Exit.",
                )
            return
        self.settings_window.close()
        self.audio_recorder.close()
        event.accept()
        QApplication.quit()  # the tray keeps the app alive past its last window
