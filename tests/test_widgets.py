from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never a real window

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ui.widgets import Segmented, ShellDialog, ToggleRow, stylesheet

LEFT = Qt.MouseButton.LeftButton
NO_KEYS = Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication(["test", "-platform", "offscreen"])


class SettingsLike(ShellDialog):
    """A Settings-style dialog that counts the presses ShellDialog turns into a window drag."""

    def __init__(self) -> None:
        super().__init__(None, "Settings")
        self.drags = 0
        self.row = ToggleRow("Write a debug log to disk", "Event names and timings only.")
        self.body.addWidget(self.row)
        self.row.resize(400, 44)

    def mousePressEvent(self, event) -> None:
        self.drags += 1  # where ShellDialog calls startSystemMove()
        super().mousePressEvent(event)


def test_a_press_on_a_switch_row_never_drags_the_dialog(app) -> None:
    # A system move ends with a release Qt fakes over the row, which flipped the switch.
    dialog = SettingsLike()
    title = dialog.row.findChildren(QLabel)[0]

    for target, at in ((title, QPoint(5, 5)), (dialog.row, QPoint(330, 22))):
        QTest.mousePress(target, LEFT, NO_KEYS, at)
        QTest.mouseRelease(target, LEFT, NO_KEYS, at)

    assert dialog.drags == 0


def test_a_release_the_row_never_saw_pressed_does_not_toggle(app) -> None:
    row = ToggleRow("Start with Windows")
    row.resize(400, 44)

    QTest.mouseRelease(row, LEFT, NO_KEYS, QPoint(10, 20))

    assert not row.switch.isChecked()


def test_dragging_across_a_row_does_not_toggle(app) -> None:
    row = ToggleRow("Keep running in tray when closed")
    row.resize(400, 44)

    QTest.mousePress(row, LEFT, NO_KEYS, QPoint(10, 20))
    QTest.mouseMove(row, QPoint(200, 22))
    QTest.mouseRelease(row, LEFT, NO_KEYS, QPoint(200, 22))

    assert not row.switch.isChecked()


def test_a_click_on_a_row_toggles_it_once(app) -> None:
    row = ToggleRow("Restore the clipboard")
    row.resize(400, 44)

    QTest.mouseClick(row, LEFT, NO_KEYS, QPoint(10, 20))
    assert row.switch.isChecked()
    QTest.mousePress(row, LEFT, NO_KEYS, QPoint(10, 20))
    QTest.mouseRelease(row, LEFT, NO_KEYS, QPoint(10, 60))  # let go below the row
    assert row.switch.isChecked()


def test_keyboard_focus_shows_on_segment_buttons(app) -> None:
    host = QWidget()
    host.setStyleSheet(stylesheet())
    strip = Segmented([("Hold to talk", "hold"), ("Toggle", "toggle")], host)
    strip.set_value("hold")
    host.show()  # offscreen platform
    host.activateWindow()
    app.processEvents()

    for value in ("toggle", "hold"):  # unchecked, then the orange checked one
        segment = strip.buttons[value]
        plain = segment.grab().toImage()
        segment.setFocus(Qt.FocusReason.TabFocusReason)
        app.processEvents()
        assert segment.hasFocus()
        assert segment.grab().toImage() != plain, value
        segment.clearFocus()
    host.close()
