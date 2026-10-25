"""Theme and custom widgets, after LumaDeck's window chrome and controls.

The chrome and control shapes follow LumaDeck's App.xaml (rounded shell,
panels, pill toggles, segmented strips); the colours are OLED black with
neutral greys, orange for selection and action, cyan for keyboard focus.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QKeyEvent,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

COLORS = {
    "shell": (0, 0, 0, 255),
    "text": "#F2F2F2",
    "muted": "#A3A3A3",
    "faint": "#777777",
    "caption": "#C8C8C8",
    "accent": "#EA8A2B",
    "accent_light": "#FFB877",
    "glow": "#FF9D45",
    "primary": "#E5842A",
    "on_accent": "#140B03",
    "cyan": "#55C7D8",
    "border": "#222222",
    "field": "#000000",
    "button": "#181818",
    "button_border": "#2C2C2C",
    "toggle_off": "#2A2A2A",
    "toggle_hover": "#363636",
    "knob_off": "#D4D4D4",
    "warning": "#FFB35C",
    "error": "#FF9C8F",
    "close_hover": "#C42B1C",
}

SHADOW = 14  # transparent margin around the shell that carries the drop shadow
SHELL_RADIUS = 10
ICON_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
GLYPHS = {
    "settings": "",
    "minimize": "",
    "maximize": "",
    "restore": "",
    "close": "",
    "refresh": "",
    "mic": "",
    "copy": "",
    "folder": "",
}


def _first_family(candidates: tuple[str, ...]) -> str:
    families = set(QFontDatabase.families())
    return next((family for family in candidates if family in families), "")


def icon_font(size: int) -> QFont | None:
    family = _first_family(ICON_FAMILIES)
    if not family:
        return None
    font = QFont(family)
    font.setPixelSize(size)
    return font


def display_font(size: int, weight: QFont.Weight = QFont.Weight.DemiBold) -> QFont:
    font = QFont(_first_family(("Segoe UI Variable Display", "Segoe UI")) or "Segoe UI")
    font.setPixelSize(size)
    font.setWeight(weight)
    return font


def stylesheet() -> str:
    icon_family = _first_family(ICON_FAMILIES) or "Segoe UI"
    return f"""
    QWidget {{ color: #F2F2F2; font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 13px; }}
    #panel {{ background: #0A0A0A; border: 1px solid #1F1F1F; border-radius: 8px; }}
    #glass {{ background: #111111; border: 1px solid #1F1F1F; border-radius: 8px; }}
    #inset {{ background: #000000; border: 1px solid #262626; border-radius: 6px; }}
    #windowTitle {{ font-size: 14px; font-weight: 600; }}
    #columnTitle {{ font-family: "Segoe UI Variable Display", "Segoe UI"; font-size: 19px; font-weight: 600; }}
    #dialogTitle {{ font-family: "Segoe UI Variable Display", "Segoe UI"; font-size: 22px; font-weight: 600; }}
    #cardTitle {{ font-family: "Segoe UI Variable Display", "Segoe UI"; font-weight: 600; }}
    #section {{ font-weight: 600; }}
    #muted {{ color: #A3A3A3; }}
    #small {{ color: #8C8C8C; font-size: 11px; }}
    #status {{ color: #55C7D8; font-size: 11px; font-weight: 600; }}
    #cyan {{ color: #55C7D8; font-weight: 600; }}
    #amberValue {{ color: #EA8A2B; font-size: 18px; font-weight: 600; }}
    #strong {{ font-weight: 600; }}
    #warning {{ color: #FFB35C; }}
    #error {{ color: #FF9C8F; }}
    #divider {{ background: #1C1C1C; max-height: 1px; min-height: 1px; border: none; }}

    QPushButton {{ background: #181818; color: #F2F2F2; border: 1px solid #2C2C2C; border-radius: 6px;
        padding: 8px 12px; font-size: 13px; }}
    QPushButton:hover {{ background: #202020; border-color: #3A3A3A; }}
    QPushButton:pressed {{ background: #121212; }}
    QPushButton:focus {{ border-color: #55C7D8; }}
    QPushButton:disabled {{ color: #5E5E5E; background: #101010; border-color: #1E1E1E; }}
    QPushButton#primary {{ background: #E5842A; color: #140B03; border-color: #E5842A; font-weight: 600; }}
    QPushButton#primary:hover {{ background: #EF9442; border-color: #EF9442; }}
    QPushButton#primary:pressed {{ background: #C8711F; }}
    QPushButton#primary:disabled {{ background: #3A240F; color: #8C6A4A; border-color: #3A240F; }}
    QPushButton#icon {{ font-family: "{icon_family}"; font-size: 11px; padding: 0px;
        min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; }}
    QPushButton#caption, QPushButton#captionClose {{ background: transparent; border: none; border-radius: 0px;
        color: #C8C8C8; font-family: "{icon_family}"; font-size: 11px; padding: 0px;
        min-width: 46px; max-width: 46px; min-height: 42px; max-height: 42px; }}
    QPushButton#caption:hover {{ background: rgba(255, 255, 255, 26); color: #FFFFFF; }}
    QPushButton#captionClose {{ border-top-right-radius: 9px; }}
    QPushButton#captionClose:hover {{ background: #C42B1C; color: #FFFFFF; }}
    QPushButton#dialogClose {{ background: transparent; border: none; color: #C8C8C8; font-family: "{icon_family}";
        font-size: 10px; padding: 0px; min-width: 34px; max-width: 34px; min-height: 30px; max-height: 30px; }}
    QPushButton#dialogClose:hover {{ background: rgba(255, 255, 255, 26); color: #FFFFFF; }}
    QPushButton#segment {{ background: transparent; border: 1px solid transparent; border-radius: 4px;
        color: #A3A3A3; font-size: 12px; font-weight: 600; padding: 7px 8px; margin: 1px; }}
    QPushButton#segment:hover {{ background: #1A1A1A; color: #F2F2F2; }}
    QPushButton#segment:checked {{ background: #EA8A2B; border-color: #EA8A2B; color: #140B03; }}

    QLineEdit, QPlainTextEdit {{ background: #000000; color: #F2F2F2; border: 1px solid #2B2B2B; border-radius: 6px;
        padding: 6px 8px; selection-background-color: #EA8A2B; selection-color: #140B03; }}
    QLineEdit:hover, QPlainTextEdit:hover {{ border-color: #444444; }}
    QLineEdit:focus, QPlainTextEdit:focus {{ border-color: #55C7D8; }}
    QComboBox {{ background: #000000; color: #F2F2F2; border: 1px solid #2B2B2B; border-radius: 4px;
        padding: 6px 38px 6px 8px; min-height: 20px; }}
    QComboBox:hover {{ background: #0E0E0E; border-color: #444444; }}
    QComboBox:on {{ background: #0E0E0E; border-color: #EA8A2B; }}
    QComboBox:focus {{ border-color: #55C7D8; }}
    QComboBox:disabled {{ color: #5E5E5E; }}
    QComboBox::drop-down {{ border: none; width: 30px; }}
    QComboBox::down-arrow {{ image: none; width: 0px; height: 0px; }}
    QComboBox QAbstractItemView {{ background: #0D0D0D; color: #F2F2F2; border: 1px solid #333333;
        padding: 3px; outline: 0px; }}
    QComboBox QAbstractItemView::item {{ padding: 7px 10px; border: 1px solid transparent; border-radius: 4px; }}
    QComboBox QAbstractItemView::item:hover {{ background: #1C1C1C; border-color: #333333; }}
    QComboBox QAbstractItemView::item:selected {{ background: #2A1A0B; border-color: #EA8A2B; color: #FFE6CF; }}
    QListWidget {{ background: #000000; border: 1px solid #222222; border-radius: 6px; padding: 2px; outline: 0px; }}
    QListWidget::item {{ border: 1px solid transparent; border-radius: 4px; padding: 6px 10px; margin: 1px 0px; }}
    QListWidget::item:hover {{ background: rgba(255, 255, 255, 20); }}
    QListWidget::item:selected {{ background: rgba(234, 138, 43, 38); border-color: #EA8A2B; color: #F2F2F2; }}

    QScrollArea, #scrollContent {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: #0A0A0A; width: 10px; border-radius: 5px; margin: 0px; }}
    QScrollBar:vertical:hover {{ background: #111111; }}
    QScrollBar::handle:vertical {{ background: #333333; border: 1px solid #444444; border-radius: 4px;
        min-height: 22px; margin: 2px; }}
    QScrollBar::handle:vertical:hover {{ background: #4A4A4A; border-color: #5A5A5A; }}
    QScrollBar::handle:vertical:pressed {{ background: #EA8A2B; border-color: #FFB877; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{ background: #0A0A0A; height: 10px; border-radius: 5px; margin: 0px; }}
    QScrollBar::handle:horizontal {{ background: #333333; border: 1px solid #444444; border-radius: 4px;
        min-width: 22px; margin: 2px; }}
    QScrollBar::handle:horizontal:hover {{ background: #4A4A4A; border-color: #5A5A5A; }}
    QScrollBar::handle:horizontal:pressed {{ background: #EA8A2B; border-color: #FFB877; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
    QProgressBar {{ background: #222222; border: none; border-radius: 2px; min-height: 4px; max-height: 4px; }}
    QProgressBar::chunk {{ background: #EA8A2B; border-radius: 2px; }}
    QSlider {{ min-height: 20px; }}
    QSlider::groove:horizontal {{ background: #222222; height: 4px; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: #EA8A2B; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: #EA8A2B; border-top: 2px solid #FFB877; width: 12px; height: 10px;
        margin: -4px 0px; border-radius: 2px; }}
    QSlider::handle:horizontal:hover {{ background: #FFB877; border-top-color: #FFE3C4; }}
    QToolTip {{ background: #0D0D0D; color: #F2F2F2; border: 1px solid #333333; padding: 6px; }}
    """


def label(text: str = "", name: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    return line


def button(text: str, name: str = "") -> QPushButton:
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        widget.setObjectName(name)
    return widget


def paint_shell(painter: QPainter, outer: QRectF, margin: float, radius: float) -> None:
    """LumaDeck's RootShell shape in OLED black: rounded, hairline, soft shadow.

    The shadow ring is drawn as concentric strokes; its faintest pixels keep
    a little alpha so the margin still catches the mouse for resizing.
    """
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    shell = outer.adjusted(margin, margin, -margin, -margin)
    if margin > 0:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 1))
        painter.drawRoundedRect(outer, radius + margin, radius + margin)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        steps = int(margin)
        for step in range(steps):
            fade = 1 - step / steps
            painter.setPen(QPen(QColor(0, 0, 0, int(70 * fade * fade)), 1.2))
            grow = step + 0.5
            painter.drawRoundedRect(
                shell.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow
            )
    painter.setPen(QPen(QColor("#262626"), 1))
    painter.setBrush(QColor(*COLORS["shell"]))
    painter.drawRoundedRect(shell.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


class SafeComboBox(QComboBox):
    """LumaDeck's combo: divider and chevron on the right; the mouse wheel
    only scrolls the list once it is open, so scrolling never changes a setting."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setView(QListView())
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)

    def wheelEvent(self, event) -> None:
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = self.width() - 30
        mid = self.height() / 2
        painter.setPen(QPen(QColor("#333333"), 1))
        painter.drawLine(QPointF(x + 0.5, mid - 9), QPointF(x + 0.5, mid + 9))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#BDBDBD"))
        cx = x + 15
        painter.drawPolygon(
            QPolygonF([QPointF(cx - 4.5, mid - 2.5), QPointF(cx + 4.5, mid - 2.5), QPointF(cx, mid + 2.5)])
        )


class Switch(QAbstractButton):
    """LumaDeck's pill toggle, with the QCheckBox API the settings code uses."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)

    def sizeHint(self) -> QSize:
        return QSize(44, 24)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        on = self.isChecked()
        track = COLORS["accent"] if on else (
            COLORS["toggle_hover"] if self.underMouse() else COLORS["toggle_off"]
        )
        painter.setPen(QPen(QColor(COLORS["cyan"]), 2) if self.hasFocus() else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(track))
        painter.drawRoundedRect(QRectF(0.5, 0.5, 43, 23), 11.5, 11.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FFFFFF" if on else COLORS["knob_off"]))
        painter.drawEllipse(QRectF(23 if on else 3, 3, 18, 18))

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class ToggleRow(QWidget):
    """Label on the left, pill on the right; the whole row toggles."""

    def __init__(self, title: str, description: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(label(title))
        if description:
            text.addWidget(label(description, "small", wrap=True))
        layout.addLayout(text, 1)
        self.switch = Switch()
        layout.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.switch.toggle()
        super().mouseReleaseEvent(event)


class Segmented(QFrame):
    """LumaDeck's automation-mode selector: an inset strip, orange when chosen."""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inset")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        for text, value in options:
            choice = button(text, "segment")
            choice.setCheckable(True)
            self.group.addButton(choice)
            self.buttons[value] = choice
            layout.addWidget(choice, 1)
            choice.clicked.connect(lambda checked=False, v=value: self.changed.emit(v))

    def value(self) -> str:
        for value, choice in self.buttons.items():
            if choice.isChecked():
                return value
        return next(iter(self.buttons))

    def set_value(self, value: str) -> None:
        choice = self.buttons.get(value) or next(iter(self.buttons.values()))
        choice.setChecked(True)


class Meter(QWidget):
    """Five small segments for the speed/accuracy ratings."""

    def __init__(self, value: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.value = value
        self.setFixedSize(5 * 13, 12)

    def set_value(self, value: int) -> None:
        self.value = value
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(5):
            colour = QColor(COLORS["accent"] if index < self.value else "#2A2A2A")
            paint_cube(painter, QPointF(index * 13 + 5.5, 6.5), 10, colour)


def paint_cube(painter: QPainter, centre: QPointF, size: float, colour: QColor) -> None:
    """A tiny isometric voxel: lit top, plain left face, shaded right face."""
    half, quarter = size / 2, size / 4
    faces = (
        ([QPointF(0, -half), QPointF(half, -quarter), QPointF(0, 0), QPointF(-half, -quarter)], colour.lighter(135)),
        ([QPointF(-half, -quarter), QPointF(0, 0), QPointF(0, half), QPointF(-half, quarter)], colour),
        ([QPointF(0, 0), QPointF(half, -quarter), QPointF(half, quarter), QPointF(0, half)], colour.darker(145)),
    )
    for points, tone in faces:
        painter.setBrush(tone)
        painter.drawPolygon(QPolygonF([centre + point for point in points]))


class CubeTitle(QWidget):
    """A section heading with a small orange voxel in front of it."""

    def __init__(self, text: str, name: str = "section", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 0, 0)
        layout.setSpacing(0)
        self.label = label(text, name)
        layout.addWidget(self.label)
        layout.addStretch(1)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        paint_cube(painter, QPointF(5.5, self.height() / 2), 10, QColor(COLORS["accent"]))


class _FitScrollArea(QScrollArea):
    """Asks for its content's full height (QScrollArea caps its hint near 400 px)."""

    def sizeHint(self) -> QSize:
        widget = self.widget()
        if widget is None:
            return super().sizeHint()
        hint = widget.sizeHint()
        if widget.hasHeightForWidth():  # wrapped labels need their real height at this width
            hint.setHeight(max(hint.height(), widget.heightForWidth(self.viewport().width() or hint.width())))
        frame = 2 * self.frameWidth()
        return hint + QSize(frame, frame)


class ShellDialog(QDialog):
    """A LumaDeck-style frameless dialog: glass shell, title, close glyph.

    Drag it by any empty part of the shell, as in LumaDeck's settings window.
    """

    def __init__(self, parent: QWidget | None, title: str, subtitle: str = "", width: int = 460) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(title)
        self.setFixedWidth(width + 2 * SHADOW)
        if parent is not None:
            self.setStyleSheet(parent.styleSheet() or stylesheet())
        else:
            self.setStyleSheet(stylesheet())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW + 22, SHADOW + 16, SHADOW + 22, SHADOW + 20)
        outer.setSpacing(0)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label(title, "dialogTitle"))
        if subtitle:
            titles.addWidget(label(subtitle, "muted", wrap=True))
        header.addLayout(titles, 1)
        close = button(GLYPHS["close"], "dialogClose")
        close.setToolTip("Close")
        close.clicked.connect(self.reject)
        header.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)
        outer.addSpacing(16)
        content = QWidget()
        content.setObjectName("scrollContent")
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(12)
        scroll = _FitScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll, 1)

    def showEvent(self, event) -> None:
        # Never taller than the screen: past that the body scrolls instead of squeezing.
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            self.setMaximumHeight(screen.availableGeometry().height())
        super().showEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        paint_shell(painter, QRectF(self.rect()), SHADOW, SHELL_RADIUS)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle() is not None:
            self.windowHandle().startSystemMove()
        super().mousePressEvent(event)


def show_message(
    parent: QWidget | None,
    title: str,
    text: str,
    buttons: tuple[str, ...] = ("OK",),
) -> bool:
    """A LumaDeck-style message box; True when the first (primary) button is chosen."""
    dialog = ShellDialog(parent, title, width=420)
    message = label(text, "muted", wrap=True)
    message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    dialog.body.addWidget(message)
    row = QHBoxLayout()
    row.addStretch(1)
    for index, text_value in reversed(list(enumerate(buttons))):
        choice = button(text_value, "primary" if index == 0 else "")
        choice.clicked.connect(dialog.accept if index == 0 else dialog.reject)
        row.addWidget(choice)
    dialog.body.addSpacing(6)
    dialog.body.addLayout(row)
    return dialog.exec() == QDialog.DialogCode.Accepted


# --- shortcut capture -------------------------------------------------------


def _qt_key_value(key: Qt.Key) -> int:
    return int(key.value)


QT_TRIGGER_KEYS = {
    _qt_key_value(Qt.Key.Key_Space): "Space",
    _qt_key_value(Qt.Key.Key_Return): "Enter",
    _qt_key_value(Qt.Key.Key_Enter): "Enter",
    _qt_key_value(Qt.Key.Key_Tab): "Tab",
    _qt_key_value(Qt.Key.Key_Escape): "Esc",
    _qt_key_value(Qt.Key.Key_Backspace): "Backspace",
    _qt_key_value(Qt.Key.Key_Delete): "Delete",
    _qt_key_value(Qt.Key.Key_Insert): "Insert",
    _qt_key_value(Qt.Key.Key_Home): "Home",
    _qt_key_value(Qt.Key.Key_End): "End",
    _qt_key_value(Qt.Key.Key_PageUp): "PageUp",
    _qt_key_value(Qt.Key.Key_PageDown): "PageDown",
    _qt_key_value(Qt.Key.Key_Up): "Up",
    _qt_key_value(Qt.Key.Key_Down): "Down",
    _qt_key_value(Qt.Key.Key_Left): "Left",
    _qt_key_value(Qt.Key.Key_Right): "Right",
    _qt_key_value(Qt.Key.Key_Semicolon): "Semicolon",
    _qt_key_value(Qt.Key.Key_Equal): "Equals",
    _qt_key_value(Qt.Key.Key_Comma): "Comma",
    _qt_key_value(Qt.Key.Key_Minus): "Minus",
    _qt_key_value(Qt.Key.Key_Period): "Period",
    _qt_key_value(Qt.Key.Key_Slash): "Slash",
    _qt_key_value(Qt.Key.Key_QuoteLeft): "Backtick",
    _qt_key_value(Qt.Key.Key_BracketLeft): "LeftBracket",
    _qt_key_value(Qt.Key.Key_Backslash): "Backslash",
    _qt_key_value(Qt.Key.Key_BracketRight): "RightBracket",
    _qt_key_value(Qt.Key.Key_Apostrophe): "Quote",
}

QT_MODIFIER_KEYS = {
    _qt_key_value(Qt.Key.Key_Control): "Ctrl",
    _qt_key_value(Qt.Key.Key_Shift): "Shift",
    _qt_key_value(Qt.Key.Key_Alt): "Alt",
    _qt_key_value(Qt.Key.Key_Meta): "Win",
}

NATIVE_KEY_NAMES = {
    0xBA: "Semicolon",
    0xBB: "Equals",
    0xBC: "Comma",
    0xBD: "Minus",
    0xBE: "Period",
    0xBF: "Slash",
    0xC0: "Backtick",
    0xDB: "LeftBracket",
    0xDC: "Backslash",
    0xDD: "RightBracket",
    0xDE: "Quote",
}


MOUSE_BUTTON_NAMES = {
    Qt.MouseButton.LeftButton: "M1",
    Qt.MouseButton.RightButton: "M2",
    Qt.MouseButton.MiddleButton: "M3",
    Qt.MouseButton.BackButton: "M4",
    Qt.MouseButton.ForwardButton: "M5",
}


class ShortcutCaptureInput(QLineEdit):
    """Click, press the combination, release: the field records the shortcut.

    Mouse buttons count too: once capturing, pressing M3/M4/M5 over the box
    (or M1/M2 together with a modifier) records that button.
    """

    capture_started = Signal()
    shortcut_captured = Signal(str)
    capture_cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._previous_text = ""
        self._pressed_tokens: set[str] = set()
        self._last_tokens: set[str] = set()
        self._mouse_pending = False
        self.setPlaceholderText("Click, then press keys or a mouse button")
        self.setToolTip(
            "Click the box, then press a key combination or a mouse button.\n"
            "M4 and M5 are the side buttons; M1 and M2 need Ctrl, Alt, Shift or Win."
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def mousePressEvent(self, event) -> None:
        name = MOUSE_BUTTON_NAMES.get(event.button())
        if self._capturing and name:
            tokens = self._modifier_names(event.modifiers())
            if name not in ("M1", "M2") or tokens:
                tokens.add(name)
                self._last_tokens = tokens
                self.setText(self._format_tokens(tokens))
                self._mouse_pending = True
            event.accept()
            return
        super().mousePressEvent(event)
        self.start_capture()

    def mouseReleaseEvent(self, event) -> None:
        if self._capturing and self._mouse_pending:
            self._mouse_pending = False
            self.complete_capture()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self._capturing:  # a right-click here is a binding, not a menu
            event.accept()
            return
        super().contextMenuEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._capturing:
            self.start_capture()
        if event.isAutoRepeat():
            event.accept()
            return
        if event.key() == _qt_key_value(Qt.Key.Key_Escape) and not self._modifier_names(
            event.modifiers()
        ):
            self.cancel_capture()
            event.accept()
            return
        tokens = self._modifier_names(event.modifiers())
        key_name = self._key_name(event)
        if key_name:
            tokens.add(key_name)
            self._pressed_tokens.add(key_name)
        for modifier in tokens.intersection({"Ctrl", "Shift", "Alt", "Win"}):
            self._pressed_tokens.add(modifier)
        if tokens:
            self._last_tokens = tokens
            self.setText(self._format_tokens(tokens))
        else:
            self.setText("Unsupported key")
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if not self._capturing:
            super().keyReleaseEvent(event)
            return
        if not event.isAutoRepeat():
            key_name = self._key_name(event)
            if key_name:
                self._pressed_tokens.discard(key_name)
            modifier_name = QT_MODIFIER_KEYS.get(event.key())
            if modifier_name:
                self._pressed_tokens.discard(modifier_name)
            if not self._pressed_tokens and self._last_tokens:
                self.complete_capture()
        event.accept()

    def focusOutEvent(self, event) -> None:
        if self._capturing:
            self.cancel_capture()
        super().focusOutEvent(event)

    def start_capture(self) -> None:
        if self._capturing:
            return
        self._capturing = True
        self._previous_text = self.text()
        self._pressed_tokens.clear()
        self._last_tokens.clear()
        self.setReadOnly(True)
        self.setText("Press shortcut...")
        self.selectAll()
        self.capture_started.emit()

    def complete_capture(self) -> None:
        shortcut = self._format_tokens(self._last_tokens)
        self._capturing = False
        self.setReadOnly(False)
        self.setText(shortcut)
        self.shortcut_captured.emit(shortcut)

    def cancel_capture(self) -> None:
        self._capturing = False
        self._pressed_tokens.clear()
        self._last_tokens.clear()
        self.setReadOnly(False)
        self.setText(self._previous_text)
        self.capture_cancelled.emit()

    def _modifier_names(self, modifiers) -> set[str]:
        names: set[str] = set()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            names.add("Ctrl")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            names.add("Shift")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            names.add("Alt")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            names.add("Win")
        return names

    def _key_name(self, event: QKeyEvent) -> str | None:
        key = event.key()
        if key in QT_MODIFIER_KEYS:
            return QT_MODIFIER_KEYS[key]
        native_vk = event.nativeVirtualKey()
        if 0x30 <= native_vk <= 0x39 or 0x41 <= native_vk <= 0x5A:
            return chr(native_vk)
        if 0x70 <= native_vk <= 0x7B:
            return f"F{native_vk - 0x6F}"
        if native_vk in NATIVE_KEY_NAMES:
            return NATIVE_KEY_NAMES[native_vk]
        if _qt_key_value(Qt.Key.Key_A) <= key <= _qt_key_value(Qt.Key.Key_Z):
            return chr(ord("A") + key - _qt_key_value(Qt.Key.Key_A))
        if _qt_key_value(Qt.Key.Key_0) <= key <= _qt_key_value(Qt.Key.Key_9):
            return chr(ord("0") + key - _qt_key_value(Qt.Key.Key_0))
        if _qt_key_value(Qt.Key.Key_F1) <= key <= _qt_key_value(Qt.Key.Key_F12):
            return f"F{1 + key - _qt_key_value(Qt.Key.Key_F1)}"
        return QT_TRIGGER_KEYS.get(key)

    def _format_tokens(self, tokens: set[str]) -> str:
        ordered = [name for name in ("Ctrl", "Shift", "Alt", "Win") if name in tokens]
        triggers = sorted(
            token for token in tokens if token not in {"Ctrl", "Shift", "Alt", "Win"}
        )
        return "+".join(ordered + triggers)
