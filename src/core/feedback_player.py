from __future__ import annotations

from pathlib import Path

import winsound
from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect

from core.app_icon import resource_path

# Trimmed WAVs: the original start cue opened with 0.6 s of silence, so it
# always sounded well after the shortcut was pressed.
BUNDLED_START_SOUND_PATH = Path("assets") / "sounds" / "start.wav"
BUNDLED_STOP_SOUND_PATH = Path("assets") / "sounds" / "stop.wav"
DEFAULT_FEEDBACK_VOLUME = 0.5  # until the Sound effects slider moves


class FeedbackPlayer(QObject):
    """Start/stop cues, preloaded so they play the moment the shortcut moves.

    WAV cues go through QSoundEffect, which keeps decoded PCM ready for
    low-latency playback. Custom cues in other formats fall back to a media
    player whose source is loaded once, not on every press.
    """

    def __init__(
        self,
        start_sound_path: str | Path | None,
        stop_sound_path: str | Path | None,
        volume: float = DEFAULT_FEEDBACK_VOLUME,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._volume = max(0.0, min(1.0, volume))
        self._cues = {
            True: self._load(start_sound_path, BUNDLED_START_SOUND_PATH),
            False: self._load(stop_sound_path, BUNDLED_STOP_SOUND_PATH),
        }

    def _load(self, configured_path: str | Path | None, bundled_path: Path):
        path = Path(configured_path) if configured_path else None
        if path is None or not path.exists():
            path = resource_path(bundled_path)
        if not path.exists():
            return None
        if path.suffix.lower() == ".wav":
            effect = QSoundEffect(self)
            effect.setSource(QUrl.fromLocalFile(str(path)))
            effect.setVolume(self._volume)
            return effect
        output = QAudioOutput(self)
        output.setVolume(self._volume)
        player = QMediaPlayer(self)
        player.setAudioOutput(output)
        player.setSource(QUrl.fromLocalFile(str(path)))
        return player

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        for cue in self._cues.values():
            if isinstance(cue, QSoundEffect):
                cue.setVolume(self._volume)
            elif isinstance(cue, QMediaPlayer) and cue.audioOutput() is not None:
                cue.audioOutput().setVolume(self._volume)

    @Slot(bool)
    def play(self, active: bool) -> None:
        if self._volume <= 0:
            return  # slider at 0: silent, and no fallback beep either
        cue = self._cues.get(active)
        if isinstance(cue, QSoundEffect):
            cue.stop()
            cue.play()
            return
        if isinstance(cue, QMediaPlayer):
            cue.setPosition(0)
            cue.play()
            return
        winsound.MessageBeep(
            winsound.MB_ICONASTERISK if active else winsound.MB_ICONEXCLAMATION
        )
