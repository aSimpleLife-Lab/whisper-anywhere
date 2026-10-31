from __future__ import annotations

from pathlib import Path

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
