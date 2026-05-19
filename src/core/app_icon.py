from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

APP_ICON_PATH = Path("assets") / "icon.ico"


def resource_path(relative_path: Path) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base_path / relative_path


def app_icon() -> QIcon:
    return QIcon(str(resource_path(APP_ICON_PATH)))
