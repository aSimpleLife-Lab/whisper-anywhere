from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from core import voz_engine as ve
from core.transcriber import read_wav

DATA = Path(__file__).parent / "data"


def _model_root() -> str | None:
    candidates = [
        os.environ.get("VOZ_MODEL_ROOT", ""),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Whisper Anywhere" / "models"),
        r"F:\Whisper-Anywhere-Windows\models",
    ]
    for root in candidates:
        if root and not ve.missing_files(root):
            return root
    return None


def test_encoder_inputs_follow_the_fused_frontend_layout() -> None:
    window = np.arange(3 * ve.SAMPLE_RATE, dtype=np.float32)
    rows, mel_mask, key_bias = ve.encoder_inputs(window)

    mel_valid = window.size // ve.HOP + 1
    frames = key_bias.shape[1]
    assert frames == -(-mel_valid // ve.SUBSAMPLING)
    assert rows.shape == (1, ve.HOP, 1, frames * ve.SUBSAMPLING + 3)
    assert mel_mask.shape == (1, 1, 1, frames * ve.SUBSAMPLING)
    assert mel_mask.sum() == mel_valid
    # Sample n lands in row (n + 256) // 160 at offset (n + 256) % 160.
    n = 12_345
    shifted = n + ve.FRONT_PAD
    assert rows[0, shifted % ve.HOP, 0, shifted // ve.HOP] == window[n]


def test_a_full_window_keeps_the_published_encoder_shape() -> None:
    _rows, _mel_mask, key_bias = ve.encoder_inputs(np.zeros(ve.WINDOW_SAMPLES, np.float32))

    assert key_bias.shape[1] == ve.ENC_FRAMES


def test_long_audio_is_cut_in_the_quiet_gap() -> None:
    rng = np.random.default_rng(0)
    audio = rng.normal(0, 0.3, 20 * ve.SAMPLE_RATE).astype(np.float32)
    gap = 13 * ve.SAMPLE_RATE
    audio[gap : gap + ve.SAMPLE_RATE // 2] = 0.0

    windows = ve.split_windows(audio)

    assert len(windows) == 2
    assert all(window.size <= ve.WINDOW_SAMPLES for window in windows)
    assert gap <= windows[0].size <= gap + ve.SAMPLE_RATE // 2
    assert sum(window.size for window in windows) == audio.size


def test_fluent_speech_is_cut_at_its_short_pause_not_the_search_edge() -> None:
    # Word gaps in fluent speech are far shorter than the smoothing; the
    # zero-padded edges of the search looked quieter and cut mid-word at 11 s.
    rng = np.random.default_rng(0)
    audio = rng.normal(0, 0.3, 20 * ve.SAMPLE_RATE).astype(np.float32)
    gap = 13 * ve.SAMPLE_RATE
    audio[gap : gap + 960] = 0.0  # 60 ms

    first = ve.split_windows(audio)[0]

    assert abs(first.size - gap) < 0.15 * ve.SAMPLE_RATE


def test_tdt_steps_advance_like_nemo_greedy_decoding() -> None:
    lane = ve._Lane(frames=10)

    assert ve._step(lane, ve.BLANK, 0) == ve._BLANK_ONE_FRAME  # a blank never stalls
    assert lane.t == 1
    assert ve._step(lane, ve.BLANK, 3) == ve._BLANK_SKIP
    assert lane.t == 4
    assert ve._step(lane, 42, 2) == 42
    assert (lane.t, lane.token) == (6, 42)
    for _ in range(ve.MAX_SYMBOLS_PER_FRAME - 1):
        assert ve._step(lane, 7, 0) == 7
    assert lane.t == 6
    ve._step(lane, 7, 0)
    assert lane.t == 7  # forced onwards after too many symbols on one frame


@pytest.mark.skipif(
    _model_root() is None or not (DATA / "hello.wav").exists(),
    reason="needs the Voz bundle and the local tests/data/hello.wav recording",
)
def test_voz_transcribes_a_known_sentence() -> None:
    model = ve.VozModel(_model_root())

    text = model.transcribe(read_wav(str(DATA / "hello.wav")))

    assert text == "Hello world, this is a quick test."
    assert model.transcribe(np.zeros(800, np.float32)) == ""
