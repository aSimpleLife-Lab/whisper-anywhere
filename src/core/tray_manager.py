from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from core.app_icon import app_icon
from core.model_manager import ModelManager
from core.text_inserter import foreground_is_fullscreen

# The dot on the tray icon, and what the tooltip says after "Whisper Anywhere: ".
BADGES = {
    "listening": ("#EA8A2B", "listening"),
    "busy": ("#55C7D8", "working"),
    "error": ("#E5484D", "something went wrong"),
    "paused": ("#777777", "dictation is off"),
}
TOOLTIP_LIMIT = 127  # Windows cuts a tray tooltip there
SAME_PROBLEM_SECONDS = 60.0  # the same problem again this soon is not announced twice


class TrayManager(QObject):
    start_requested = Signal()
    stop_requested = Signal()
    open_requested = Signal()
    settings_requested = Signal()
    exit_requested = Signal()
    model_selected = Signal(str)
    dictation_toggled = Signal(bool)  # asked for in the menu; the window's switch decides

    def __init__(
        self, model_manager: ModelManager, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.model_manager = model_manager
        self._base_icon = self._make_icon()
        self._state_icons: dict[str, QIcon] = {}
        self.tray = QSystemTrayIcon(self._base_icon, self)
        self.tray.setToolTip("Whisper Anywhere")
        self.model_actions: dict[str, QAction] = {}
        self._current_model = model_manager.selected_model()
        self._dictation = True
        self._state = "idle"
        self._problem = ""  # why the last dictation failed, for the tooltip
        self._announced: tuple[str, float] = ("", 0.0)
        self._build_menu()
        self.tray.activated.connect(self._on_activated)

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def shutdown(self) -> None:
        self.tray.hide()

    def set_current_model(self, model_name: str) -> None:
        self._current_model = model_name
        for name, action in self.model_actions.items():
            action.setChecked(name == model_name)

    def _pick_model(self, model_name: str) -> None:
        # Qt ticks the item as it is clicked; put the tick back, so it moves only when
        # the window reports the switch and a refused or cancelled pick never stays ticked.
        self.set_current_model(self._current_model)
        self.model_selected.emit(model_name)

    def set_state(self, state: str) -> None:
        """Badge the tray icon: amber while listening, cyan while working, red
        after a problem, grey while dictation is off."""
        self._state = state
        if state not in BADGES and not self._dictation:
            state = "paused"
        if state not in BADGES:
            self.tray.setIcon(self._base_icon)
            self.tray.setToolTip("Whisper Anywhere")
            return
        colour, words = BADGES[state]
        if state not in self._state_icons:
            pixmap = self._base_icon.pixmap(64, 64)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor("#000000"))
            painter.setBrush(QColor(colour))
            painter.drawEllipse(34, 34, 28, 28)
            painter.end()
            self._state_icons[state] = QIcon(pixmap)
        self.tray.setIcon(self._state_icons[state])
        tooltip = f"Whisper Anywhere: {(state == 'error' and self._problem) or words}"
        if len(tooltip) > TOOLTIP_LIMIT:
            tooltip = tooltip[: TOOLTIP_LIMIT - 1].rstrip() + "…"
        self.tray.setToolTip(tooltip)

    def set_dictation(self, enabled: bool) -> None:
        """The window's Dictation switch moved: the menu and the icon follow."""
        self._dictation = enabled
        self.dictation_action.setChecked(enabled)
        self.set_state(self._state)

    def show_problem(self, heading: str, detail: str) -> None:
        """Why a dictation was not typed. With the window in the tray, the
        error sound was all there was: now the icon turns red, its tooltip
        says why, and a notification does too unless a game or the app's own
        window is in front."""
        self._problem = detail
        self.set_state("error")
        last, when = self._announced
        if self._can_announce() and (detail != last or time.monotonic() - when > SAME_PROBLEM_SECONDS):
            self._announced = (detail, time.monotonic())
            self.tray.showMessage(heading, detail, QSystemTrayIcon.MessageIcon.Warning, 6000)

    def _can_announce(self) -> bool:
        return (
            self.tray.isVisible()
            and QApplication.activeWindow() is None  # the window shows it itself
            and not foreground_is_fullscreen()  # never over a game
        )

    def show_message(self, title: str, message: str) -> None:
        if self.tray.isVisible():
            self.tray.showMessage(
                title, message, QSystemTrayIcon.MessageIcon.Information, 3500
            )

    def _build_menu(self) -> None:
        menu = QMenu()

        # Pausing the shortcut meant opening the window first.
        self.dictation_action = QAction("Dictation on", self)
        self.dictation_action.setCheckable(True)
        self.dictation_action.setChecked(True)
        self.dictation_action.triggered.connect(lambda checked: self.dictation_toggled.emit(checked))
        menu.addAction(self.dictation_action)
        menu.addSeparator()

        start_action = QAction("Start listening", self)
        start_action.triggered.connect(
            lambda checked=False: self.start_requested.emit()
        )
        menu.addAction(start_action)

        stop_action = QAction("Stop listening", self)
        stop_action.triggered.connect(lambda checked=False: self.stop_requested.emit())
        menu.addAction(stop_action)

        model_menu = menu.addMenu("Current model")
        for info in self.model_manager.models():
            action = QAction(info.label, self)
            action.setCheckable(True)
            action.setChecked(info.name == self._current_model)
            action.triggered.connect(
                lambda checked=False, name=info.name: self._pick_model(name)
            )
            self.model_actions[info.name] = action
            model_menu.addAction(action)

        menu.addSeparator()

        open_action = QAction("Open app", self)
        open_action.triggered.connect(lambda checked=False: self.open_requested.emit())
        menu.addAction(open_action)

        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(
            lambda checked=False: self.settings_requested.emit()
        )
        menu.addAction(settings_action)

        menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(lambda checked=False: self.exit_requested.emit())
        menu.addAction(exit_action)

        self.tray.setContextMenu(menu)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_requested.emit()

    def _make_icon(self) -> QIcon:
        icon = app_icon()
        if not icon.isNull():
            return icon
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#7c3aed"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(8, 8, 48, 48)
        painter.setPen(QColor("#ffffff"))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(22)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "W")
        painter.end()
        return QIcon(pixmap)
