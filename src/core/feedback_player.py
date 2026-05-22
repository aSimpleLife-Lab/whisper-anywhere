from __future__ import annotations

from pathlib import Path

import winsound
from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from core.app_icon import resource_path

BUNDLED_START_SOUND_PATH = Path("assets") / "sounds" / "startsound.mp3"
BUNDLED_STOP_SOUND_PATH = Path("assets") / "sounds" / "stopsound.mp3"


class FeedbackPlayer(QObject):
    def __init__(
        self,
        start_sound_path: str | Path | None,
        stop_sound_path: str | Path | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.start_sound_path = self._resolve_sound_path(
            start_sound_path, BUNDLED_START_SOUND_PATH
        )
        self.stop_sound_path = self._resolve_sound_path(
            stop_sound_path, BUNDLED_STOP_SOUND_PATH
        )
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(1.0)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)

    def _resolve_sound_path(
        self, configured_path: str | Path | None, bundled_path: Path
    ) -> Path | None:
        if configured_path:
            path = Path(configured_path)
            if path.exists():
                return path
        path = resource_path(bundled_path)
        return path if path.exists() else None

    @Slot(bool)
    def play(self, active: bool) -> None:
        sound_path = self.start_sound_path if active else self.stop_sound_path
        if sound_path and sound_path.exists():
            self._player.stop()
            self._player.setSource(QUrl.fromLocalFile(str(sound_path)))
            self._player.setPosition(0)
            self._player.play()
            return
        winsound.MessageBeep(
            winsound.MB_ICONASTERISK if active else winsound.MB_ICONEXCLAMATION
        )
