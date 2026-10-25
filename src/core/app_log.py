"""Diagnostics log kept in memory; written to disk only when debug logs are on.

Nothing the user says is ever logged: only events, sizes and timings.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from pathlib import Path

_lines: deque[str] = deque(maxlen=500)
_lock = threading.Lock()
_disk_path: Path | None = None


def write_to_disk(path: Path | None) -> None:
    global _disk_path
    with _lock:
        _disk_path = path


def disk_path() -> Path | None:
    return _disk_path


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + f" {message}"
    with _lock:
        _lines.append(line)
        if _disk_path is None:
            return
        try:
            _disk_path.parent.mkdir(parents=True, exist_ok=True)
            with _disk_path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError:
            pass


def recent(count: int = 60) -> list[str]:
    with _lock:
        return list(_lines)[-count:]
