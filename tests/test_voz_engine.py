from __future__ import annotations

import json
import os
import re
from pathlib import Path

import numpy as np
import onnxruntime
import pytest

from core import voz_engine as ve
from core import voz_graphs as vg
from core.transcriber import read_wav

DATA = Path(__file__).parent / "data"
SR = ve.SAMPLE_RATE
W = ve.Word


def _model_root() -> str | None:
    # Only a model folder named for testing: loading Voz may rebuild the cached
    # CPU graphs, which a test run must never do to the live install.
    root = os.environ.get("WA_VOZ_TEST_MODELS", "")
    return root if root and not ve.missing_files(root) else None


needs_model = pytest.mark.skipif(
    _model_root() is None,
    reason="set WA_VOZ_TEST_MODELS to a Voz model folder that may be rewritten",
)


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


def test_recordings_up_to_15_s_stay_one_window() -> None:
    assert ve.window_starts(np.zeros(ve.WINDOW_SAMPLES, np.float32)) == [0]


def test_long_audio_runs_full_windows_that_overlap_by_up_to_3_s() -> None:
    rng = np.random.default_rng(0)
    audio = rng.normal(0, 0.3, 50 * SR).astype(np.float32)

    starts = ve.window_starts(audio)

    assert starts[0] == 0
    assert all(
        ve.WINDOW_SAMPLES - ve.BOUNDARY_SEARCH < later - earlier < ve.WINDOW_SAMPLES
        for earlier, later in zip(starts, starts[1:])
    )
    assert starts[-2] + ve.WINDOW_SAMPLES < audio.size <= starts[-1] + ve.WINDOW_SAMPLES


def test_the_next_window_starts_late_in_a_pause() -> None:
    # Every frame of a silent pause is as quiet as the next; the latest wins,
    # as in the reference, so the overlap is no longer than it needs to be.
    rng = np.random.default_rng(0)
    audio = rng.normal(0, 0.3, 20 * SR).astype(np.float32)
    gap = 13 * SR
    audio[gap : gap + SR // 2] = 0.0

    assert ve.window_starts(audio) == [0, gap + SR // 2 - ve.BOUNDARY_FRAME // 2]


def test_a_short_pause_between_words_is_found() -> None:
    rng = np.random.default_rng(0)
    audio = rng.normal(0, 0.3, 20 * SR).astype(np.float32)
    gap = 13 * SR
    audio[gap : gap + 960] = 0.0  # 60 ms

    assert gap <= ve.window_starts(audio)[1] < gap + 960


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


class _ScriptedDecoder:
    """Answers each decoder call with the next (token, duration) for both columns."""

    def __init__(self, script) -> None:
        self.script = list(script)

    def run(self, _outputs, feed):
        lanes = feed["embed"].shape[0]
        columns = self.script.pop(0)
        tok = np.array([[[token] for token, _ in columns]] * lanes, np.int64)
        dur = np.array([[[duration] for _, duration in columns]] * lanes, np.int64)
        state = np.zeros((lanes, ve.STATE_DIM, 1, 1), np.float32)
        return tok, dur, state, state


def _bare_model(decoder, encoder=None, pieces=()) -> ve.VozModel:
    model = ve.VozModel.__new__(ve.VozModel)
    model.encoder, model.decoder, model.pieces = encoder, decoder, list(pieces)
    model.embedding = np.broadcast_to(np.zeros(ve.EMBED_DIM, np.float32), (ve.BLANK + 1, ve.EMBED_DIM))
    return model


def test_decoding_times_each_token_by_the_encoder_frame_it_came_from() -> None:
    model = _bare_model(
        _ScriptedDecoder(
            [
                [(ve.BLANK, 1), (5, 2)],  # a one-frame blank, then a token from the next frame
                [(6, 0), (0, 0)],  # a token that stays on its frame
                [(ve.BLANK, 3), (0, 0)],  # a blank that skips three frames
                [(7, 4), (0, 0)],
            ]
        )
    )

    (emitted,) = model._decode([np.zeros((ve.EMBED_DIM, 10), np.float32)], [10])

    assert emitted == [(5, 1, 3), (6, 3, 3), (7, 6, 10)]  # (token, frame, end frame)


def test_words_run_from_their_first_piece_to_the_end_of_their_last() -> None:
    pieces = ["", "▁Hel", "lo", "▁world", "."]  # "" is a control piece
    emitted = [(1, 2, 4), (2, 4, 5), (0, 5, 9), (3, 9, 11), (4, 12, 13)]

    words = ve.timed_words(pieces, emitted, offset=12.0)

    assert [word.text for word in words] == ["Hello", "world."]
    assert words[0].start == pytest.approx(12.16) and words[0].end == pytest.approx(12.40)
    assert words[1].start == pytest.approx(12.72) and words[1].end == pytest.approx(13.04)


def _spoken(text: str, start: float) -> list[ve.Word]:
    """Words one every 0.3 s from `start`, as a window would time them."""
    return [W(word, start + 0.3 * i, start + 0.3 * i + 0.25) for i, word in enumerate(text.split())]


def _text(words: list[ve.Word]) -> str:
    return " ".join(word.text for word in words)


def test_overlapping_windows_join_where_their_words_agree() -> None:
    # The run-on passage: a cut inside "together" gave "company to Together
    # with". Both windows hear the seam; each word must survive exactly once.
    earlier = _spoken("sent them to the shipping company together with the order number and the rec", 10.0)
    later = _spoken("Together with the order number and the receipt from the store", 11.88)

    joined = ve.splice_overlap(earlier, later, boundary=11.7)

    assert _text(joined) == (
        "sent them to the shipping company together with the order number and the receipt from the store"
    )


def test_the_earlier_window_keeps_its_reading_of_the_seam() -> None:
    # It heard the sentence go on; the later window began cold at the seam and
    # read a sentence start: "$1,249.99. Which is".
    earlier = _spoken("The invoice total was $1,249.99, which is 12.5% more than the", 10.0)
    later = _spoken("Which is 12.5% more than the quote.", 11.58)

    joined = ve.splice_overlap(earlier, later, boundary=11.4)

    assert _text(joined) == "The invoice total was $1,249.99, which is 12.5% more than the quote."


def test_without_agreement_the_seam_is_cut_on_time_and_a_doubled_word_dropped() -> None:
    earlier = [W("Meet", 10.0, 10.2), W("me", 10.3, 10.4), W("at", 11.9, 12.2)]
    later = [W("at", 12.0, 12.2), W("noon.", 12.3, 12.7)]

    assert _text(ve.splice_overlap(earlier, later, boundary=11.95)) == "Meet me at noon."


def test_a_word_said_twice_is_kept() -> None:
    earlier = [W("I", 10.0, 10.1), W("think", 10.2, 10.5), W("that", 11.5, 11.7)]
    later = [W("that", 12.0, 12.2), W("works.", 12.3, 12.7)]

    assert _text(ve.splice_overlap(earlier, later, boundary=11.9)) == "I think that that works."


def test_a_seam_leaves_no_false_sentence_break() -> None:
    # "harbor market. organized": the earlier window punctuated where its audio ended.
    left = [W("the", 35.0, 35.2), W("harbor", 35.3, 35.6), W("market.", 35.7, 36.0)]
    assert _text(ve._repair_join(left, [W("organized", 36.2, 36.6)])) == "the harbor market organized"
    # A window starting mid-sentence capitalises the seam word: "episode. Episode. The".
    left = [W("first", 5.0, 5.3), W("episode.", 5.4, 5.9)]
    right = [W("Episode.", 5.5, 5.9), W("The", 6.1, 6.2)]
    assert _text(ve._repair_join(left, right)) == "first episode. The"


class _RampEncoder:
    """Projections carrying each frame's absolute position, read off a ramp signal."""

    def run(self, _outputs, feed):
        first_sample = feed["audio_rows"][0, ve.FRONT_PAD % ve.HOP, 0, ve.FRONT_PAD // ve.HOP]
        frames = feed["key_bias"].shape[1]
        projections = np.zeros((1, ve.EMBED_DIM, 1, frames), np.float32)
        projections[0, 0, 0] = round(float(first_sample) * 1e6) / (ve.HOP * ve.SUBSAMPLING) + np.arange(frames)
        return (projections,)


class _WordEverySixFrames:
    """Hears word k at absolute encoder frame 6k, in every window that holds it."""

    def run(self, _outputs, feed):
        frame = np.floor(feed["enc_step"][:, 0, 0, :]).astype(np.int64)
        tok = np.where(frame % 6 == 0, frame // 6, ve.BLANK)[:, :, None]
        state = np.zeros((frame.shape[0], ve.STATE_DIM, 1, 1), np.float32)
        return tok, np.ones_like(tok), state, state


def test_a_long_recording_reads_every_word_once(monkeypatch) -> None:
    samples = (np.arange(36 * SR) / 1e6).astype(np.float32)
    monkeypatch.setattr(ve, "window_starts", lambda audio: [0, 12 * SR, 24 * SR])
    model = _bare_model(
        _WordEverySixFrames(), _RampEncoder(), pieces=[f"▁w{k}" for k in range(ve.BLANK)]
    )

    assert model.transcribe(samples) == " ".join(f"w{k}" for k in range(76))


class _FakeSession:
    options: list = []
    failures = 0
    damaged = ""  # a graph name that fails to load once
    alive = 0

    def __init__(self, path, options, providers=None) -> None:
        if _FakeSession.failures or Path(path).name == _FakeSession.damaged:
            _FakeSession.failures = max(0, _FakeSession.failures - 1)
            _FakeSession.damaged = ""
            raise Exception("Deserialize tensor failed")  # what ORT raises for a damaged graph
        _FakeSession.options.append(options)
        _FakeSession.alive += 1
        self.loaded = True

    def __del__(self) -> None:
        _FakeSession.alive -= getattr(self, "loaded", False)

    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]


@pytest.fixture
def fake_runtime(tmp_path, monkeypatch):
    folder = ve.bundle_path(tmp_path)
    folder.mkdir(parents=True)
    np.zeros(2 * ve.EMBED_DIM, np.float16).tofile(folder / "embedding.f16")
    (folder / "vocab.json").write_text(json.dumps(["<blk>", "▁a"]), encoding="utf-8")
    builds: list[bool] = []

    def ensure(path, revision, rebuild=False):
        assert (path, revision) == (folder, ve.REVISION)
        # A live session maps encoder.cpu.data, and Windows then refuses to rewrite it.
        assert not (rebuild and _FakeSession.alive), "a session still holds the cache"
        builds.append(rebuild)
        return path / vg.ENCODER_CPU, path / vg.DECODER_CPU

    monkeypatch.setattr(ve, "missing_files", lambda root: [])
    monkeypatch.setattr(ve, "ensure_cpu_graphs", ensure)
    monkeypatch.setattr(onnxruntime, "InferenceSession", _FakeSession)
    _FakeSession.options, _FakeSession.failures, _FakeSession.damaged = [], 0, ""
    _FakeSession.alive = 0
    return builds


def test_voz_threads_spin_only_briefly_after_each_run(tmp_path, fake_runtime) -> None:
    # On 16 threads a 7 s dictation burned 5.3 CPU-seconds, mostly spinning,
    # while a game wanted the cores; a 0.2 ms spin cut that to 3.5.
    ve.VozModel(tmp_path, cpu_threads=6)

    assert len(_FakeSession.options) == 2
    for options in _FakeSession.options:
        assert options.get_session_config_entry("session.intra_op.spin_duration_us") == "200"
        assert options.intra_op_num_threads == 6


def test_a_cache_that_fails_to_load_is_rebuilt_once(tmp_path, fake_runtime) -> None:
    _FakeSession.failures = 1

    ve.VozModel(tmp_path)

    assert fake_runtime == [False, True]


def test_the_encoder_lets_go_of_the_cache_before_a_rebuild(tmp_path, fake_runtime) -> None:
    _FakeSession.damaged = vg.DECODER_CPU  # the encoder loads, the decoder does not

    ve.VozModel(tmp_path)

    assert fake_runtime == [False, True]


def test_a_cache_that_fails_again_after_a_rebuild_is_reported(tmp_path, fake_runtime) -> None:
    _FakeSession.failures = 3

    with pytest.raises(Exception, match="Deserialize"):
        ve.VozModel(tmp_path)
    assert fake_runtime == [False, True]


def _speech(text: str, folder: Path) -> np.ndarray:
    """Windows' own voice, written straight to a 16 kHz WAV, never to the speakers."""
    client = pytest.importorskip("win32com.client")
    path = folder / "speech.wav"
    try:
        audio_format = client.Dispatch("SAPI.SpAudioFormat")
        audio_format.Type = 18  # SAFT16kHz16BitMono
        stream = client.Dispatch("SAPI.SpFileStream")
        stream.Format = audio_format
        stream.Open(str(path), 3)  # SSFMCreateForWrite
        voice = client.Dispatch("SAPI.SpVoice")
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
    except Exception as exc:  # no SAPI voice on this PC
        pytest.skip(f"Windows speech synthesis is unavailable: {exc}")
    return read_wav(str(path))


RUN_ON = (
    "I wanted to let you know that the package we were waiting for finally arrived this morning but "
    "the box was damaged on one side and two of the glass jars inside were broken so I took photos of "
    "everything and sent them to the shipping company together with the order number and the receipt "
    "from the store and they promised to call me back before the end of the week with an answer about "
    "the refund"
)
EMAIL = (
    "Hi Sarah, thanks for sending the draft so quickly, I read through it this morning and I think the "
    "overall structure works well, but the second section feels a bit long compared to the others, so "
    "maybe we could move the table about last year's results into the appendix and keep only the summary "
    "in the main text, I also noticed that the dates in the timeline do not match the ones in the budget "
    "sheet, could you double check which ones are correct before we send it to the board, and finally, I "
    "would like to add a short paragraph about the new partners in Lisbon, since they agreed to help with "
    "the pilot next spring."
)


@needs_model
@pytest.mark.parametrize("passage", [RUN_ON, EMAIL], ids=["run_on", "email"])
def test_long_dictations_read_across_window_seams_like_uncut_audio(passage, tmp_path) -> None:
    model = ve.VozModel(_model_root())
    samples = _speech(passage, tmp_path)
    starts = ve.window_starts(samples)
    assert len(starts) >= 2

    text = model.transcribe(samples)

    for seam in starts[1:]:
        # One window centred on the seam reads it with context on both sides.
        low, high = max(0, seam - 7 * SR), min(samples.size, seam + 7 * SR)
        rows, mel_mask, key_bias = ve.encoder_inputs(samples[low:high])
        feed = {"audio_rows": rows, "mel_mask": mel_mask, "key_bias": key_bias}
        (projections,) = model.encoder.run(None, feed)
        (emitted,) = model._decode([projections[0, :, 0, :]], [key_bias.shape[1]])
        uncut = ve.timed_words(model.pieces, emitted, low / SR)
        phrase = " ".join(word.text for word in uncut if abs(word.start - seam / SR) <= 1.5)
        assert f" {phrase} " in f" {text} "
    assert not re.search(r"\b(\w+) \1\b", text, re.IGNORECASE)  # no "at at"


@needs_model
def test_a_damaged_or_outdated_graph_cache_is_rebuilt(tmp_path) -> None:
    root = _model_root()
    folder = ve.bundle_path(root)
    speech = _speech("Can you send me the file by tomorrow? I need it for the meeting.", tmp_path)
    expected = ve.VozModel(root).transcribe(speech)
    assert expected.startswith("Can you send me the file")

    data = folder / vg.ENCODER_CPU_DATA
    with data.open("r+b") as file:
        file.truncate(data.stat().st_size // 2)  # the app stopped mid-write
    assert ve.VozModel(root).transcribe(speech) == expected

    for name in (vg.ENCODER_CPU, vg.DECODER_CPU):  # the same size, unreadable
        graph = folder / name
        graph.write_bytes(bytes(graph.stat().st_size))
        assert ve.VozModel(root).transcribe(speech) == expected

    record = folder / vg.CACHE_INFO
    stale = json.loads(record.read_text(encoding="utf-8"))
    stale["revision"] = "an older download"  # what an update to the model brings
    record.write_text(json.dumps(stale), encoding="utf-8")
    assert ve.VozModel(root).transcribe(speech) == expected
    assert json.loads(record.read_text(encoding="utf-8"))["revision"] == ve.REVISION


@pytest.mark.skipif(
    _model_root() is None or not (DATA / "hello.wav").exists(),
    reason="needs WA_VOZ_TEST_MODELS and the local tests/data/hello.wav recording",
)
def test_voz_transcribes_a_known_sentence() -> None:
    model = ve.VozModel(_model_root())

    text = model.transcribe(read_wav(str(DATA / "hello.wav")))

    assert text == "Hello world, this is a quick test."
    assert model.transcribe(np.zeros(800, np.float32)) == ""
