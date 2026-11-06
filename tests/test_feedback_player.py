from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

import core.feedback_player as feedback_player
from core.feedback_player import FeedbackPlayer

SOUNDS = Path(__file__).resolve().parents[1] / "assets" / "sounds"


class FakeEffect:
    """Stands in for QSoundEffect: records what would play and never opens an audio device."""

    made: list[FakeEffect] = []

    def __init__(self, parent=None) -> None:
        self.source = ""
        self.volume = None
        self.calls: list[str] = []
        FakeEffect.made.append(self)

    def setSource(self, url) -> None:
        self.source = Path(url.toLocalFile()).name

    def setVolume(self, volume: float) -> None:
        self.volume = volume

    def stop(self) -> None:
        self.calls.append("stop")

    def play(self) -> None:
        self.calls.append("play")


@pytest.fixture
def beeps(monkeypatch) -> list[int]:
    FakeEffect.made = []
    calls: list[int] = []
    monkeypatch.setattr(feedback_player, "QSoundEffect", FakeEffect)
    monkeypatch.setattr(feedback_player.winsound, "MessageBeep", calls.append)
    return calls


def played() -> dict[str, list[str]]:
    return {effect.source: effect.calls for effect in FakeEffect.made if effect.calls}


def test_cancel_and_error_play_their_own_bundled_cues(beeps) -> None:
    player = FeedbackPlayer("", "")

    player.play_cue("cancel")
    player.play_cue("error")
    player.play(True)
    player.play(False)

    restart = ["stop", "play"]
    assert played() == {"cancel.wav": restart, "error.wav": restart, "start.wav": restart, "stop.wav": restart}
    assert beeps == []


def test_the_volume_reaches_every_cue_and_zero_is_silent(beeps) -> None:
    player = FeedbackPlayer("", "", volume=0.5)

    player.set_volume(0.2)
    assert {effect.source: effect.volume for effect in FakeEffect.made} == dict.fromkeys(
        ("start.wav", "stop.wav", "cancel.wav", "error.wav"), 0.2
    )
    player.set_volume(0.0)
    player.play_cue("cancel")
    player.play_cue("error")
    assert played() == {}
    assert beeps == []


def test_a_missing_error_cue_falls_back_to_the_system_sound(beeps, monkeypatch) -> None:
    monkeypatch.setattr(feedback_player, "BUNDLED_ERROR_SOUND_PATH", Path("assets") / "sounds" / "missing.wav")
    player = FeedbackPlayer("", "")

    player.play_cue("error")

    assert beeps == [feedback_player.winsound.MB_ICONHAND]


def read(name: str) -> tuple[tuple[int, int, int], float, np.ndarray]:
    with wave.open(str(SOUNDS / name)) as cue:
        layout = (cue.getframerate(), cue.getnchannels(), cue.getsampwidth())
        samples = np.frombuffer(cue.readframes(cue.getnframes()), "<i2").reshape(-1, cue.getnchannels()) / 32768
    return layout, len(samples) / layout[0], samples.mean(axis=1)


def centroid(mono: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(mono))
    return float((np.fft.rfftfreq(len(mono), 1 / 48000) * spectrum).sum() / spectrum.sum())


def test_the_new_cues_are_short_and_no_louder_than_start_and_stop() -> None:
    start_layout, _, start = read("start.wav")
    for name in ("cancel.wav", "error.wav"):
        layout, seconds, mono = read(name)
        assert layout == start_layout
        assert seconds <= 0.5
        assert np.abs(mono).max() <= np.abs(start).max()


def test_the_error_cue_is_a_low_double_tone() -> None:
    mono = read("error.wav")[2]
    envelope = np.abs(mono[: len(mono) // 480 * 480]).reshape(-1, 480).max(axis=1)  # 10 ms steps
    loud = envelope > envelope.max() / 2
    bursts = int(loud[0]) + int(np.sum(loud[1:] & ~loud[:-1]))

    assert bursts == 2
    assert centroid(mono) < 0.6 * min(centroid(read("start.wav")[2]), centroid(read("stop.wav")[2]))
