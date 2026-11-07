from __future__ import annotations

import ctypes
import json
import os
import stat
from ctypes import wintypes
from pathlib import Path

import pytest

from core import app_log


def test_logs_and_recordings_left_by_v0_1_are_destroyed(tmp_path, monkeypatch) -> None:
    # v0.1 wrote the text of every dictation to activity.log, and nothing since
    # removed it, while the app says nothing you say is kept.
    config = tmp_path / "config"
    temp = tmp_path / "temp"
    (temp / "Whisper Anywhere").mkdir(parents=True)
    config.mkdir()
    left = [config / "activity.log", config / "hotkey.log", temp / "Whisper Anywhere" / "recording-1.wav"]
    for path in left:
        path.write_bytes(b"secret words")
    (config / "settings.json").write_text("{}", encoding="utf-8")
    last_contents: list[bytes] = []
    real_unlink = Path.unlink

    def unlink(path: Path, missing_ok: bool = False) -> None:
        last_contents.append(path.read_bytes())
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    app_log.destroy_legacy_files(config, temp)

    assert not any(path.exists() for path in left)
    assert (config / "settings.json").exists()
    assert len(last_contents) == 3
    assert b"secret words" not in last_contents  # overwritten before the delete


def test_clips_v0_2_0_to_0_2_3_left_in_temp_are_destroyed_too(tmp_path) -> None:
    # PL-6: FunASR got its audio as %TEMP%\wa-<8 random>.wav (tempfile.mkstemp),
    # and a crash mid-transcription left it there.
    config = tmp_path / "config"
    config.mkdir()
    clip = tmp_path / "wa-k3j2x_9q.wav"
    clip.write_bytes(b"RIFF....WAVEfmt speech samples")
    not_ours = [tmp_path / "wa-notes.wav", tmp_path / "wave-k3j2x_9q.wav", tmp_path / "wa-k3j2x_9q.txt"]
    for path in not_ours:
        path.write_bytes(b"someone else's")

    app_log.destroy_legacy_files(config, tmp_path)

    assert not clip.exists()
    assert all(path.read_bytes() == b"someone else's" for path in not_ours)


GENERIC_READ = 0x80000000
FILE_SHARE_READ, FILE_SHARE_DELETE = 1, 4
OPEN_EXISTING = 3


def open_shared(path: Path, share: int) -> int:
    """Another program's handle on path, sharing only what share allows."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, share, None, OPEN_EXISTING, 0, None)
    assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    return handle


def close(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(handle)


def test_a_file_that_cannot_be_overwritten_is_never_deleted_unread(tmp_path) -> None:
    # B9: a reader sharing read+delete but not write: the overwrite failed and
    # the fallback deleted the file with every word still on the disk.
    log = tmp_path / "activity.log"
    log.write_bytes(b"dictated: my bank PIN is 4321\n")
    handle = open_shared(log, FILE_SHARE_READ | FILE_SHARE_DELETE)
    try:
        app_log.destroy_file(log)  # never raises

        assert log.read_bytes() == b"dictated: my bank PIN is 4321\n"  # left for the next start
    finally:
        close(handle)

    app_log.destroy_file(log)  # the next start
    assert not log.exists()


def test_a_file_held_without_delete_sharing_is_overwritten_and_tried_again(tmp_path) -> None:
    # B9: rename and delete failed, and the error stopped the app's start.
    log = tmp_path / "activity.log"
    log.write_bytes(b"dictated: my bank PIN is 4321\n")
    holder = open(log, "ab")  # any ordinary open by another program: no delete sharing
    try:
        app_log.destroy_legacy_files(tmp_path, tmp_path)

        assert b"PIN" not in log.read_bytes()
    finally:
        holder.close()

    app_log.destroy_legacy_files(tmp_path, tmp_path)
    assert not log.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_read_only_legacy_log_is_destroyed_without_stopping_the_start(tmp_path) -> None:
    # B9: a read-only activity.log (restored from a backup) raised out of main().
    log = tmp_path / "activity.log"
    log.write_bytes(b"dictated: my bank PIN is 4321\n")
    os.chmod(log, stat.S_IREAD)

    app_log.destroy_legacy_files(tmp_path, tmp_path)

    assert not log.exists()


@pytest.fixture
def user_zoe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Zoe")
    monkeypatch.setenv("USERNAME", "Zoe")


def test_shared_text_carries_neither_the_profile_folder_nor_the_user_name(user_zoe) -> None:
    # B22: Copy/Save diagnostics said "no personal paths" and held C:\Users\<name>.
    payload = {
        "model_path": r"C:\Users\Zoe\AppData\Local\Whisper Anywhere\models",
        "log": "loaded c:/users/zoe/Music/start.wav",
        "microphone": "Microphone (Zoe's AirPods)",
    }
    text = json.dumps(payload)  # JSON doubles every backslash

    scrubbed = app_log.scrub_user_paths(text)

    assert "zoe" not in scrubbed.lower()
    assert json.loads(scrubbed) == {
        "model_path": r"%USERPROFILE%\AppData\Local\Whisper Anywhere\models",
        "log": "loaded %USERPROFILE%/Music/start.wav",
        "microphone": "Microphone (%USERNAME%'s AirPods)",
    }


def test_scrubbing_leaves_other_words_and_users_alone(user_zoe) -> None:
    text = r"Zoetrope C:\Users\Zoey\x Ozone zoe"

    assert app_log.scrub_user_paths(text) == r"Zoetrope C:\Users\Zoey\x Ozone %USERNAME%"
