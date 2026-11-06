"""Voz speech recognition on ONNX Runtime.

Voz (desert-ant-labs/voz on Hugging Face) is NVIDIA Parakeet TDT 0.6B v3
converted by Desert Ant Labs. Its official SDKs keep the pipeline in a
WebAssembly/Swift core and only hand ONNX Runtime two graphs, so this module
rebuilds that core (desert-ant-core Pipeline.swift and Words.swift): the
fused-frontend input layout, overlapping 15 s windows that start at pauses,
TDT greedy decoding with one decoder lane per window, and a join of the
windows where their words agree.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

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
FRAME_SECONDS = HOP * SUBSAMPLING / SAMPLE_RATE  # 80 ms: the grid every word time lands on
# Long recordings run as full 15 s windows. Each next one starts at the quietest
# 20 ms in the last 3 s of the one before, so neighbours share up to 3 s that
# both transcribe; cutting without that overlap broke sentences and words.
BOUNDARY_SEARCH = 3 * SAMPLE_RATE
BOUNDARY_FRAME = SAMPLE_RATE // 50
NEAR_FLOOR = 2.0  # frames up to twice the quietest one's energy are pauses too
MATCH_TOLERANCE = 1.0  # seconds; further apart, the same word is another occurrence
MINIMUM_RUN = 2  # one matching word is coincidence: "the" is everywhere

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
# SHA-256 of each file at REVISION (the LFS oid; for the two JSON files, of the
# bytes whose git blob id Hugging Face lists), so a download is checked whole.
BUNDLE_SHA256 = {
    "meta.json": "2fb6b111dcf4f2477d7ff7699b2374c5f7194ae5fa20ef2a06f121c243119a37",
    "vocab.json": "3eb2f73e8853dce5156c2ed97c0b1bb84eb4ebd201eb1761d15977fc1ea31490",
    "embedding.f16": "07163e5f6f8a68f9ffeed7a8ff770c68098c2db216232321912188bd614b9a72",
    "encoder.onnx": "ac316927d8b4d726932ea876617447847a57e35b2053db6b42db6868a91f8f05",
    "encoder.onnx.data": "3338df284d31ba23e94ef8f0c397e7fa69a36dc634b4569a4739f94d72a38228",
    "decoder.onnx": "9bf9cb1557f98ab2e5b3ff982fa605827354efc20f2b247c722cab9adf10bd4f",
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


HF_BASE = "https://huggingface.co"
HF_FILES_URL = f"{HF_BASE}/{REPO_ID}/tree/{REVISION}/{BUNDLE_DIR}"
READ_TIMEOUT = 30.0  # seconds without any data before an attempt gives up
ATTEMPTS = 3
# A prepare the window gave up on may still be downloading; a second one must
# not write the same .part files alongside it.
_download_lock = threading.Lock()


def download_bundle(model_root: str | Path) -> Path:
    """Fetch the ONNX bundle (about 385 MB) pinned to a known revision.

    A plain resumable HTTP download rather than huggingface_hub: its hidden
    retries kept a blocked network on "0 MB" for minutes and then reported only
    a closed client. Here a silent network fails within about a minute and a
    half, and the error says why. urllib uses the Windows proxy settings and
    certificate store, so antivirus HTTPS scanning still works.
    """
    with _download_lock:
        folder = bundle_path(model_root)
        folder.mkdir(parents=True, exist_ok=True)
        for name in missing_files(model_root):
            _download_file(f"{BUNDLE_DIR}/{name}", folder / name, BUNDLE_FILES[name], BUNDLE_SHA256[name])
    missing = missing_files(model_root)
    if missing:
        raise VozError(f"The Voz download is incomplete: {', '.join(missing)}")
    return folder


def _download_file(remote: str, target: Path, size: int, sha256: str) -> None:
    part = target.with_name(target.name + ".part")
    url = f"{HF_BASE}/{REPO_ID}/resolve/{REVISION}/{remote}"
    context = ssl.create_default_context()  # loads the Windows certificate store
    last_error: Exception | None = None
    for attempt in range(ATTEMPTS):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        headers = {"User-Agent": "Whisper Anywhere"}
        if have:
            headers["Range"] = f"bytes={have}-"  # resume what an earlier try left
        try:
            if have < size:  # a complete .part only lacks its rename; more gets HTTP 416
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=READ_TIMEOUT, context=context) as response:
                    if have and response.status != 206:
                        have = 0  # the server ignored the range: start over
                    with part.open("ab" if have else "wb") as out:
                        while chunk := response.read(1 << 20):
                            out.write(chunk)
            if part.stat().st_size != size:
                last_error = VozError(f"{remote} arrived incomplete")
            else:
                with part.open("rb") as written:
                    intact = hashlib.file_digest(written, "sha256").hexdigest() == sha256
                if intact:
                    os.replace(part, target)
                    return
                part.unlink()  # the next try starts over instead of resuming foreign bytes
                last_error = VozError(
                    f"{remote} did not match the published file. A proxy, Wi-Fi sign-in page "
                    "or antivirus may have changed it on the way."
                )
        except (OSError, http.client.HTTPException) as exc:  # URLError and timeouts are OSErrors
            last_error = exc
        if attempt + 1 < ATTEMPTS:
            time.sleep(2 * (attempt + 1))
    raise VozError(describe_download_error(last_error)) from last_error


def describe_download_error(exc: BaseException | None) -> str:
    """Turn a network failure into a reason a person can act on."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"huggingface.co answered with error {exc.code}."
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, TimeoutError):
        return (
            "huggingface.co did not respond. A firewall, antivirus, VPN or network filter may be "
            "blocking Whisper Anywhere, or the connection is very slow."
        )
    if isinstance(reason, socket.gaierror):
        return "This PC could not look up huggingface.co. Check the internet connection, VPN or DNS filter."
    if isinstance(reason, ssl.SSLError):
        return "The secure connection to huggingface.co failed. Antivirus HTTPS scanning or a proxy may be interfering."
    if isinstance(reason, ConnectionRefusedError):
        return "The connection to huggingface.co was refused. A firewall or proxy may be blocking it."
    if isinstance(reason, ConnectionError):
        return "The connection to huggingface.co kept dropping. Check the internet connection."
    return f"The download failed: {reason}"


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
        options = ort.SessionOptions()
        options.log_severity_level = 3
        if cpu_threads:
            options.intra_op_num_threads = cpu_threads
        # Pool threads spin while they wait for work: on 16 threads a 7 s
        # dictation burned 5.3 CPU-seconds, mostly spinning, while a game
        # wanted the cores. A 0.2 ms spin cut that to 3.5 at the same speed.
        options.add_session_config_entry("session.intra_op.spin_duration_us", "200")
        providers = providers or ["CPUExecutionProvider"]
        for rebuild in (False, True):
            encoder_path, decoder_path = ensure_cpu_graphs(folder, REVISION, rebuild)
            try:
                self.encoder = ort.InferenceSession(str(encoder_path), options, providers=providers)
                self.decoder = ort.InferenceSession(str(decoder_path), options, providers=providers)
                break
            except Exception:
                # Damaged where its sizes cannot show it: build it once more,
                # after letting go of a loaded encoder, whose mapping of
                # encoder.cpu.data Windows would not let the rebuild overwrite.
                self.encoder = None
                if rebuild:
                    raise
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
        # A last window under MIN_SAMPLES adds under 0.1 s to the one before it.
        starts = [start for start in window_starts(samples) if samples.size - start >= MIN_SAMPLES]
        encoded: list[np.ndarray] = []
        frames: list[int] = []
        for start in starts:
            rows, mel_mask, key_bias = encoder_inputs(samples[start : start + WINDOW_SAMPLES])
            (enc_proj,) = self.encoder.run(
                None, {"audio_rows": rows, "mel_mask": mel_mask, "key_bias": key_bias}
            )
            encoded.append(enc_proj[0, :, 0, :])
            frames.append(key_bias.shape[1])
        words: list[Word] = []
        for start, emitted in zip(starts, self._decode(encoded, frames)):
            # Every word a window heard is kept, past the next window's start
            # too: that overlap is what the join aligns on.
            offset = start / SAMPLE_RATE
            words = splice_overlap(words, timed_words(self.pieces, emitted, offset), offset)
        return " ".join(word.text for word in words if word.text)

    def _decode(
        self, encoded: list[np.ndarray], frames: list[int]
    ) -> list[list[tuple[int, int, int]]]:
        """TDT greedy decoding, one decoder lane per window.

        Each call runs the prediction LSTM on every lane's last token and joins
        it with two consecutive encoder frames, so a one-frame blank continues
        straight into the next frame. A lane's (h, c) is the state *before* its
        last token and only advances when a token is emitted. Every token comes
        with the encoder frame it was read at and the frame its predicted
        duration ends at, which time the words for the join.
        """
        count = len(encoded)
        lanes = [_Lane(frames=n) for n in frames]
        outputs: list[list[tuple[int, int, int]]] = [[] for _ in lanes]
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
                at, duration = lane.t, int(dur[index, 0, 0])
                emitted = _step(lane, int(tok[index, 0, 0]), duration)
                if emitted == _BLANK_ONE_FRAME and lane.t < lane.frames:
                    # Column two is the same prediction joined with the frame a
                    # one-frame blank just moved to.
                    at, duration = lane.t, int(dur[index, 1, 0])
                    emitted = _step(lane, int(tok[index, 1, 0]), duration)
                if emitted >= 0:
                    outputs[index].append((emitted, at, at + duration))
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


def window_starts(samples: np.ndarray) -> list[int]:
    """Where each window starts (Pipeline.swift nextBoundary).

    Every window but the last runs a full 15 s. The next one starts at the
    quietest 20 ms in the last 3 s of it, so it begins where the speaker
    paused, as the utterances the model was trained on do.
    """
    starts = [0]
    while samples.size > starts[-1] + WINDOW_SAMPLES:
        end = starts[-1] + WINDOW_SAMPLES
        region = samples[end - BOUNDARY_SEARCH : end].astype(np.float64)
        energy = np.square(region).reshape(-1, BOUNDARY_FRAME).mean(axis=1)
        centres = end - BOUNDARY_SEARCH + BOUNDARY_FRAME * np.arange(energy.size) + BOUNDARY_FRAME // 2
        # Of the frames about as quiet as the quietest, a later one is cheaper:
        # it leaves the next window less audio to repeat.
        quiet = energy <= NEAR_FLOOR * max(float(energy.min()), 1e-9)
        score = np.where(quiet, energy * (1 + (end - centres) / BOUNDARY_SEARCH), np.inf)
        starts.append(int(centres[score.size - 1 - int(np.argmin(score[::-1]))]))  # last of equals
    return starts


class Word(NamedTuple):
    text: str
    start: float  # seconds from the start of the recording
    end: float


def timed_words(pieces: list[str], emitted: list[tuple[int, int, int]], offset: float) -> list[Word]:
    """Group a window's tokens into words (Words.swift timedWords).

    A word starts at the frame of its first piece and ends where its last
    piece's predicted duration does; `offset` is the window's start in seconds.
    """
    words: list[Word] = []
    text, start, end = "", 0, 0
    for token, frame, until in emitted:
        piece = pieces[token] if 0 <= token < len(pieces) else ""
        if not piece:
            continue  # control pieces are not words
        if piece.startswith("▁") and text:
            words.append(Word(text, offset + start * FRAME_SECONDS, offset + end * FRAME_SECONDS))
            text = ""
        if not text:
            start = frame
        end = max(end, until)
        text += piece.replace("▁", "")
    if text:
        words.append(Word(text, offset + start * FRAME_SECONDS, offset + end * FRAME_SECONDS))
    return words


def splice_overlap(previous: list[Word], following: list[Word], boundary: float) -> list[Word]:
    """Join a window's words onto those before it (Words.swift spliceOverlap).

    Both transcribe the up to 3 s around `boundary` (where the later window
    starts). Cutting on time alone doubles any word whose two timings straddle
    the cut ("at at"), so the longest run of words both agree on becomes the
    seam, and the earlier window's copy of it is kept: it heard what came
    before, where the later one began cold. Without such a run, cut on time.
    """
    if not previous or not following:
        return previous + following
    reach = BOUNDARY_SEARCH / SAMPLE_RATE
    tail = [(i, word) for i, word in enumerate(previous) if word.start >= boundary - reach]
    head = [(i, word) for i, word in enumerate(following) if word.start < boundary + reach]
    if not tail or not head:
        return [w for w in previous if w.start < boundary] + [w for w in following if w.start >= boundary]
    best = (0, 0, 0, float("inf"))  # run length, tail index, head index, drift
    for t in range(len(tail)):
        for h in range(len(head)):
            length = 0
            while (
                t + length < len(tail)
                and h + length < len(head)
                and _matches(tail[t + length][1], head[h + length][1])
            ):
                length += 1
            if length < MINIMUM_RUN:
                continue
            drift = abs(tail[t][1].start - head[h][1].start)
            if length > best[0] or (length == best[0] and drift < best[3]):
                best = (length, t, h, drift)
    length, t, h, _ = best
    if length < MINIMUM_RUN:
        return _repair_join(
            [w for w in previous if w.start < boundary], [w for w in following if w.start >= boundary]
        )
    agreed = [word for _, word in tail[t : t + length]]
    return _repair_join(previous[: tail[t][0]] + agreed, following[head[h][0] + length :])


def _matches(a: Word, b: Word) -> bool:
    return abs(a.start - b.start) <= MATCH_TOLERANCE and _fold(a.text) == _fold(b.text)


def _fold(text: str) -> str:
    """A word as a reader compares it: case and punctuation aside."""
    return "".join(ch for ch in text.casefold() if ch.isalnum() or ch == "'")


def _repair_join(left: list[Word], right: list[Word]) -> list[Word]:
    """Clean the two one-word marks a seam leaves (Words.swift repairJoin)."""
    if not left or not right:
        return left + right
    last, first = left[-1], right[0]
    # The seam word twice: as a false sentence start ("episode. Episode."), or
    # in the same case over the same moment. "that that" said twice is kept.
    if _fold(last.text) == _fold(first.text) and (last.text != first.text or first.start < last.end):
        right = right[1:]
    # The earlier window punctuated where its audio stopped; a lower-case word
    # after it means the sentence went on: "harbor market. organized".
    if right and right[0].text[:1].islower() and last.text.endswith((".", "!", "?")):
        left = left[:-1] + [last._replace(text=last.text[:-1])]
    return left + right


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
