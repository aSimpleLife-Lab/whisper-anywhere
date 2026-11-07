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
BUNDLED_CANCEL_SOUND_PATH = Path("assets") / "sounds" / "cancel.wav"
BUNDLED_ERROR_SOUND_PATH = Path("assets") / "sounds" / "error.wav"
DEFAULT_FEEDBACK_VOLUME = 0.5  # until the Sound effects slider moves
FALLBACK_BEEPS = {  # when a cue file is missing
    "start": winsound.MB_ICONASTERISK,
    "stop": winsound.MB_ICONEXCLAMATION,
    "cancel": winsound.MB_ICONEXCLAMATION,
    "error": winsound.MB_ICONHAND,
}


class FeedbackPlayer(QObject):
    """Start, stop, cancel and error cues, preloaded so they play without delay.

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
            "start": self._load(start_sound_path, BUNDLED_START_SOUND_PATH),
            "stop": self._load(stop_sound_path, BUNDLED_STOP_SOUND_PATH),
            "cancel": self._load(None, BUNDLED_CANCEL_SOUND_PATH),
            "error": self._load(None, BUNDLED_ERROR_SOUND_PATH),
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
        self.play_cue("start" if active else "stop")

    @Slot(str)
    def play_cue(self, kind: str) -> None:
        """Play the "start", "stop", "cancel" or "error" cue."""
        if self._volume <= 0:
            return  # slider at 0: silent, and no fallback beep either
        cue = self._cues.get(kind)
        if isinstance(cue, QSoundEffect):
            cue.stop()
            cue.play()
            return
        if isinstance(cue, QMediaPlayer):
            cue.setPosition(0)
            cue.play()
            return
        if kind in FALLBACK_BEEPS:
            winsound.MessageBeep(FALLBACK_BEEPS[kind])
