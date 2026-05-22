from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from core.app_icon import app_icon
from core.model_manager import ModelManager


class TrayManager(QObject):
    start_requested = Signal()
    stop_requested = Signal()
    open_requested = Signal()
    settings_requested = Signal()
    exit_requested = Signal()
    model_selected = Signal(str)

    def __init__(
        self, model_manager: ModelManager, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.model_manager = model_manager
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip("Whisper Anywhere")
        self.model_actions: dict[str, QAction] = {}
        self._build_menu()
        self.tray.activated.connect(self._on_activated)

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def shutdown(self) -> None:
        self.tray.hide()

    def set_current_model(self, model_name: str) -> None:
        for name, action in self.model_actions.items():
            action.setChecked(name == model_name)

    def show_message(self, title: str, message: str) -> None:
        if self.tray.isVisible():
            self.tray.showMessage(
                title, message, QSystemTrayIcon.MessageIcon.Information, 3500
            )

    def _build_menu(self) -> None:
        menu = QMenu()

        start_action = QAction("Start listening", self)
        start_action.triggered.connect(
            lambda checked=False: self.start_requested.emit()
        )
        menu.addAction(start_action)

        stop_action = QAction("Stop listening", self)
        stop_action.triggered.connect(lambda checked=False: self.stop_requested.emit())
        menu.addAction(stop_action)

        model_menu = menu.addMenu("Current model")
        selected = self.model_manager.selected_model()
        for info in self.model_manager.models():
            action = QAction(info.name, self)
            action.setCheckable(True)
            action.setChecked(info.name == selected)
            action.triggered.connect(
                lambda checked=False, name=info.name: self.model_selected.emit(name)
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
