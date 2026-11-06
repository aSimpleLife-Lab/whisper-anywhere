"""Diagnostics log kept in memory; written to disk only when debug logs are on.

Nothing the user says is ever logged: only events, sizes and timings.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

# v0.1.x wrote these; activity.log holds the text of every dictation.
LEGACY_FILES = ("activity.log", "hotkey.log")
# v0.2.0-0.2.3 handed FunASR its audio as tempfile.mkstemp(prefix="wa-", suffix=".wav").
_V02_CLIP = re.compile(r"wa-[a-z0-9_]{8}\.wav")

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


def scrub_user_paths(text: str) -> str:
    """For anything shared, such as diagnostics: the profile folder becomes
    %USERPROFILE%, then the bare user name %USERNAME%. Either slash counts, and
    so do JSON's doubled backslashes."""
    profile = os.environ.get("USERPROFILE", "")
    parts = [part for part in re.split(r"[\\/]+", profile) if part]
    names = {os.environ.get("USERNAME", "")}
    if len(parts) > 1:  # a drive alone is no profile
        pattern = r"[\\/]+".join(map(re.escape, parts)) + r"(?!\w)"
        text = re.sub(pattern, "%USERPROFILE%", text, flags=re.IGNORECASE)
        names.add(parts[-1])
    for name in names - {""}:
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "%USERNAME%", text, flags=re.IGNORECASE)
    return text


def destroy_legacy_files(config_dir: Path, temp_dir: Path | None = None) -> None:
    """Destroy what older versions left behind: v0.1.x's logs, and recordings
    a crash mid-transcription left in %TEMP% (v0.1.x and v0.2.0-0.2.3)."""
    temp_dir = Path(tempfile.gettempdir()) if temp_dir is None else temp_dir
    recordings = sorted((temp_dir / "Whisper Anywhere").glob("recording-*.wav"))
    clips = sorted(path for path in temp_dir.glob("wa-*.wav") if _V02_CLIP.fullmatch(path.name))
    for path in [config_dir / name for name in LEGACY_FILES] + recordings + clips:
        if path.is_file():
            destroy_file(path)


def destroy_file(path: Path) -> None:
    """Overwrite (10 alternating 0x00/0xFF passes + 1 random), rename, delete.

    Never raises. A file that cannot be overwritten in full stays for the next
    start: deleting it unread would leave its words on the disk for whoever
    holds it open.
    """
    try:
        if not os.access(path, os.W_OK):
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)  # a read-only copy from a backup
        size = path.stat().st_size
        with path.open("r+b") as file:
            for fill in [b"\x00", b"\xff"] * 5 + [None]:
                file.seek(0)
                file.write(os.urandom(size) if fill is None else fill * size)
                file.flush()
                os.fsync(file.fileno())
    except OSError:
        return
    try:
        renamed = path.with_name(os.urandom(8).hex())
        path.replace(renamed)
        renamed.unlink()
    except OSError:
        pass  # overwritten already; held open without delete sharing, it goes next start
