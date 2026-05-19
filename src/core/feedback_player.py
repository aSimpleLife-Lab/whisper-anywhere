from __future__ import annotations

from pathlib import Path

import winsound
from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class FeedbackPlayer(QObject):
    def __init__(self, start_sound_path: str | Path | None, stop_sound_path: str | Path | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.start_sound_path = Path(start_sound_path) if start_sound_path else None
        self.stop_sound_path = Path(stop_sound_path) if stop_sound_path else None
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(1.0)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)

    @Slot(bool)
    def play(self, active: bool) -> None:
        sound_path = self.start_sound_path if active else self.stop_sound_path
        if sound_path and sound_path.exists():
            self._player.stop()
            self._player.setSource(QUrl.fromLocalFile(str(sound_path)))
            self._player.setPosition(0)
            self._player.play()
            return
        winsound.MessageBeep(winsound.MB_ICONASTERISK if active else winsound.MB_ICONEXCLAMATION)
