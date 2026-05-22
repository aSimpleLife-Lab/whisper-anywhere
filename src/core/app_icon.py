from __future__ import annotations

import sys
from ctypes import windll
from pathlib import Path

from PySide6.QtGui import QIcon

APP_ICON_PATH = Path("assets") / "icon.png"
NATIVE_ICON_PATH = Path("assets") / "icon.ico"


def resource_path(relative_path: Path) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base_path / relative_path


def app_icon() -> QIcon:
    return QIcon(str(resource_path(APP_ICON_PATH)))


def apply_native_window_icon(hwnd: int) -> None:
    if sys.platform != "win32" or not hwnd:
        return

    icon_path = str(resource_path(NATIVE_ICON_PATH))
    image_icon = 1
    lr_load_from_file = 0x00000010
    wm_seticon = 0x0080
    icon_small = 0
    icon_big = 1

    small_icon = windll.user32.LoadImageW(
        None, icon_path, image_icon, 16, 16, lr_load_from_file
    )
    big_icon = windll.user32.LoadImageW(
        None, icon_path, image_icon, 256, 256, lr_load_from_file
    )
    if small_icon:
        windll.user32.SendMessageW(hwnd, wm_seticon, icon_small, small_icon)
    if big_icon:
        windll.user32.SendMessageW(hwnd, wm_seticon, icon_big, big_icon)
