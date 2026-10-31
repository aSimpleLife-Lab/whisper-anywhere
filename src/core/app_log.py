"""Diagnostics log kept in memory; written to disk only when debug logs are on.

Nothing the user says is ever logged: only events, sizes and timings.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

# v0.1.x wrote these; activity.log holds the text of every dictation.
LEGACY_FILES = ("activity.log", "hotkey.log")

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


def destroy_legacy_files(config_dir: Path, temp_dir: Path | None = None) -> None:
    """Destroy what v0.1.x left behind: its logs and any recording a crash
    mid-transcription left in %TEMP%."""
    temp_dir = Path(tempfile.gettempdir()) if temp_dir is None else temp_dir
    recordings = sorted((temp_dir / "Whisper Anywhere").glob("recording-*.wav"))
    for path in [config_dir / name for name in LEGACY_FILES] + recordings:
        if path.is_file():
            destroy_file(path)


def destroy_file(path: Path) -> None:
    """Overwrite (10 alternating 0x00/0xFF passes + 1 random), rename, delete."""
    try:
        size = path.stat().st_size
        with path.open("r+b") as file:
            for fill in [b"\x00", b"\xff"] * 5 + [None]:
                file.seek(0)
                file.write(os.urandom(size) if fill is None else fill * size)
                file.flush()
                os.fsync(file.fileno())
        renamed = path.with_name(os.urandom(8).hex())
        path.replace(renamed)
        renamed.unlink()
    except OSError:
        path.unlink(missing_ok=True)
