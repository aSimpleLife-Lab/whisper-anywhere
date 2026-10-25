"""Voz speech recognition on ONNX Runtime.

Voz (desert-ant-labs/voz on Hugging Face) is NVIDIA Parakeet TDT 0.6B v3
converted by Desert Ant Labs. Its official SDKs keep the pipeline in a
WebAssembly/Swift core and only hand ONNX Runtime two graphs, so this module
rebuilds that core: the fused-frontend input layout, windows of at most 15 s
cut at pauses, and TDT greedy decoding with one decoder lane per window.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from core.voz_graphs import ensure_cpu_graphs

SAMPLE_RATE = 16000
HOP = 160
WINDOW_SAMPLES = 240_000  # 15 s: the longest window the positional tables cover
FRONT_PAD = 256  # n_fft // 2, so mel frame f is centred on sample f * HOP
MEL_FRAMES = 1500
SUBSAMPLING = 8  # three stride-2 convolutions
ENC_FRAMES = 188
EMBED_DIM = 640
STATE_DIM = 1280  # two LSTM layers of 640
BLANK = 8192
MAX_SYMBOLS_PER_FRAME = 10
MIN_SAMPLES = SAMPLE_RATE // 10  # the frontend needs a few frames to normalise
# Long recordings are cut at the quietest 200 ms in the last 4 s of each window,
# which lands on a pause between words or sentences.
CUT_SEARCH_SAMPLES = 4 * SAMPLE_RATE
CUT_FRAME_SAMPLES = 320
CUT_SMOOTH_FRAMES = 10

REPO_ID = "desert-ant-labs/voz"
REVISION = "12802bf4e2b5e97e1289b723bc6283b6f1a1ace5"
BUNDLE_DIR = "web"
BUNDLE_FILES = {
    "meta.json": 539,
    "vocab.json": 78_654,
    "embedding.f16": 10_487_040,
    "encoder.onnx": 813_056,
    "encoder.onnx.data": 348_981_248,
    "decoder.onnx": 24_472_430,
}
DOWNLOAD_MB = round(sum(BUNDLE_FILES.values()) / 1_000_000)


class VozError(RuntimeError):
    pass


def bundle_path(model_root: str | Path) -> Path:
    return Path(model_root).expanduser() / "voz" / BUNDLE_DIR


def missing_files(model_root: str | Path) -> list[str]:
    folder = bundle_path(model_root)
    return [
        name
        for name, size in BUNDLE_FILES.items()
        if not (folder / name).is_file() or (folder / name).stat().st_size != size
    ]


def download_bundle(model_root: str | Path) -> Path:
    """Fetch the ONNX bundle (about 385 MB) pinned to a known revision."""
    missing = missing_files(model_root)
    if not missing:
        return bundle_path(model_root)
    try:
        from huggingface_hub import hf_hub_download
    except Exception as exc:  # pragma: no cover - ships with faster-whisper
        raise VozError("huggingface_hub is missing, so Voz cannot download.") from exc
    target = Path(model_root).expanduser() / "voz"
    for name in missing:
        hf_hub_download(
            REPO_ID, f"{BUNDLE_DIR}/{name}", revision=REVISION, local_dir=target
        )
    missing = missing_files(model_root)
    if missing:
        raise VozError(f"The Voz download is incomplete: {', '.join(missing)}")
    return bundle_path(model_root)


@dataclass
class _Lane:
    frames: int
    t: int = 0
    token: int = BLANK
    symbols_here: int = 0


class VozModel:
    def __init__(
        self,
        model_root: str | Path,
        providers: list[Any] | None = None,
        cpu_threads: int | None = None,
    ) -> None:
        import onnxruntime as ort

        missing = missing_files(model_root)
        if missing:
            raise VozError(f"Voz model files are missing: {', '.join(missing)}")
        folder = bundle_path(model_root)
        encoder_path, decoder_path = ensure_cpu_graphs(folder)
        options = ort.SessionOptions()
        options.log_severity_level = 3
        if cpu_threads:
            options.intra_op_num_threads = cpu_threads
        providers = providers or ["CPUExecutionProvider"]
        self.encoder = ort.InferenceSession(
            str(encoder_path), options, providers=providers
        )
        self.decoder = ort.InferenceSession(
            str(decoder_path), options, providers=providers
        )
        self.active_provider = self.encoder.get_providers()[0]
        self.embedding = (
            np.fromfile(folder / "embedding.f16", dtype=np.float16)
            .reshape(-1, EMBED_DIM)
            .astype(np.float32)
        )
        vocab = json.loads((folder / "vocab.json").read_text(encoding="utf-8"))
        self.pieces = [
            "" if piece.startswith("<") and piece.endswith(">") else piece
            for piece in vocab
        ]

    def warm_up(self) -> None:
        """Run one short pass so the first real dictation is not the slow one."""
        self.transcribe(np.zeros(SAMPLE_RATE, np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size < MIN_SAMPLES:
            return ""
        encoded: list[np.ndarray] = []
        frames: list[int] = []
        for window in split_windows(samples):
            if window.size < MIN_SAMPLES:
                continue
            rows, mel_mask, key_bias = encoder_inputs(window)
            (enc_proj,) = self.encoder.run(
                None, {"audio_rows": rows, "mel_mask": mel_mask, "key_bias": key_bias}
            )
            encoded.append(enc_proj[0, :, 0, :])
            frames.append(key_bias.shape[1])
        if not encoded:
            return ""
        texts = [self.detokenize(ids) for ids in self._decode(encoded, frames)]
        return " ".join(text for text in texts if text)

    def detokenize(self, ids: list[int]) -> str:
        text = "".join(self.pieces[i] for i in ids if 0 <= i < len(self.pieces))
        return " ".join(text.replace("▁", " ").split())

    def _decode(self, encoded: list[np.ndarray], frames: list[int]) -> list[list[int]]:
        """TDT greedy decoding, one decoder lane per window.

        Each call runs the prediction LSTM on every lane's last token and joins
        it with two consecutive encoder frames, so a one-frame blank continues
        straight into the next frame. A lane's (h, c) is the state *before* its
        last token and only advances when a token is emitted.
        """
        count = len(encoded)
        lanes = [_Lane(frames=n) for n in frames]
        outputs: list[list[int]] = [[] for _ in lanes]
        h = np.zeros((count, STATE_DIM, 1, 1), np.float32)
        c = np.zeros((count, STATE_DIM, 1, 1), np.float32)
        embed = np.zeros((count, EMBED_DIM, 1, 1), np.float32)
        enc_step = np.zeros((count, EMBED_DIM, 1, 2), np.float32)
        while any(lane.t < lane.frames for lane in lanes):
            for index, lane in enumerate(lanes):
                if lane.t >= lane.frames:
                    continue
                embed[index, :, 0, 0] = self.embedding[lane.token]
                enc_step[index, :, 0, 0] = encoded[index][:, lane.t]
                ahead = min(lane.t + 1, lane.frames - 1)
                enc_step[index, :, 0, 1] = encoded[index][:, ahead]
            tok, dur, h_out, c_out = self.decoder.run(
                None, {"embed": embed, "h_in": h, "c_in": c, "enc_step": enc_step}
            )
            for index, lane in enumerate(lanes):
                if lane.t >= lane.frames:
                    continue
                emitted = _step(lane, int(tok[index, 0, 0]), int(dur[index, 0, 0]))
                if emitted == _BLANK_ONE_FRAME and lane.t < lane.frames:
                    # Column two is the same prediction joined with the frame a
                    # one-frame blank just moved to.
                    emitted = _step(lane, int(tok[index, 1, 0]), int(dur[index, 1, 0]))
                if emitted >= 0:
                    outputs[index].append(emitted)
                    h[index] = h_out[index]
                    c[index] = c_out[index]
        return outputs


_BLANK_ONE_FRAME = -1
_BLANK_SKIP = -2


def _step(lane: _Lane, token: int, duration: int) -> int:
    """Apply one joint result to a lane: the emitted token, or a blank code."""
    if token >= BLANK:
        advance = max(duration, 1)
        lane.t += advance
        lane.symbols_here = 0
        return _BLANK_ONE_FRAME if advance == 1 else _BLANK_SKIP
    lane.token = token
    if duration > 0:
        lane.t += duration
        lane.symbols_here = 0
    else:
        lane.symbols_here += 1
        if lane.symbols_here >= MAX_SYMBOLS_PER_FRAME:
            lane.t += 1
            lane.symbols_here = 0
    return token


def split_windows(samples: np.ndarray) -> list[np.ndarray]:
    """Cut audio into windows of at most 15 s at the quietest stretch."""
    windows: list[np.ndarray] = []
    start = 0
    while samples.size - start > WINDOW_SAMPLES:
        search_from = start + WINDOW_SAMPLES - CUT_SEARCH_SAMPLES
        region = samples[search_from : start + WINDOW_SAMPLES]
        energy = np.square(region).reshape(-1, CUT_FRAME_SAMPLES).sum(axis=1)
        smooth = np.convolve(energy, np.ones(CUT_SMOOTH_FRAMES), mode="same")
        cut = search_from + (int(np.argmin(smooth)) * CUT_FRAME_SAMPLES)
        windows.append(samples[start:cut])
        start = cut
    windows.append(samples[start:])
    return windows


def encoder_inputs(window: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Lay one window out as the fused frontend expects, sized to its length.

    The graph frames the audio as rows of HOP samples (STFT over 4 rows, one
    leading row for pre-emphasis), so T encoder frames need 8T mel frames and
    8T + 3 rows. The mel mask keeps padding out of the per-feature statistics.
    """
    length = min(window.size, WINDOW_SAMPLES)
    mel_valid = min(MEL_FRAMES, length // HOP + 1)
    frames = min(ENC_FRAMES, -(-mel_valid // SUBSAMPLING))
    mel_frames = frames * SUBSAMPLING
    rows = mel_frames + 3
    padded = np.zeros(rows * HOP, np.float32)
    padded[FRONT_PAD : FRONT_PAD + length] = window[:length]
    audio_rows = padded.reshape(1, rows, HOP).transpose(0, 2, 1)[:, :, None, :]
    mel_mask = np.zeros((1, 1, 1, mel_frames), np.float32)
    mel_mask[..., :mel_valid] = 1.0
    key_bias = np.zeros((1, frames, 1, 1), np.float32)
    return np.ascontiguousarray(audio_rows), mel_mask, key_bias
