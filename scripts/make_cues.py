"""Synthesize the start/stop cues in assets/sounds: layered voxel blips.

The core is a staircase on a 4-bit stepped triangle (16 levels, like the NES
triangle channel) plus a block "thock": start lands the block and steps up
C5-G5-C6, stop steps down and lands it. Around that: a fast chord arpeggio an
octave up on a soft pulse voice, a game-style stereo echo, a bend into the first
note, vibrato on the last, and a snap click on the thock. Peaks near -19 dBFS.
Run: python scripts/make_cues.py
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

SR = 48000
PEAK = 0.11  # about -19 dBFS; the app plays cues at half volume on top of this
SOUNDS = Path(__file__).resolve().parents[1] / "assets" / "sounds"
C5, G5, C6 = 523.25, 783.99, 1046.5
STEP = 0.055  # seconds between staircase notes


def tri16(phase: np.ndarray) -> np.ndarray:
    """Triangle rounded to 16 levels: the voxel step in the sound."""
    x = phase % 1.0
    v = np.where(x < 0.5, 4 * x - 1, 3 - 4 * x)
    return np.round((v + 1) * 7.5) / 7.5 - 1


def pulse(phase: np.ndarray) -> np.ndarray:
    """25 % pulse, the brighter chip voice."""
    return np.where(phase % 1.0 < 0.25, 0.75, -0.25)


def env(n: int, decay: float, attack: float = 0.003) -> np.ndarray:
    t = np.arange(n) / SR
    return np.clip(t / attack, 0, 1) * np.exp(-t / decay)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    a = 1 - math.exp(-2 * math.pi * cutoff / SR)
    y, s = np.empty_like(x), 0.0
    for i, v in enumerate(x):
        s += a * (v - s)
        y[i] = s
    return y


def add_note(buf, start, freq, voice, decay, bend=False, vibrato=False):
    """Mix one note into buf. bend: slides up a semitone into pitch; vibrato: wobble once it rings."""
    offset = int(start * SR)
    m = len(buf) - offset
    t = np.arange(m) / SR
    f = np.full(m, freq)
    if bend:
        f *= 2 ** (-np.exp(-t / 0.008) / 12)
    if vibrato:
        f *= 1 + 0.006 * np.sin(2 * np.pi * 7 * t) * np.clip((t - 0.04) / 0.05, 0, 1)
    buf[offset:] += voice(np.cumsum(f) / SR) * env(m, decay)


def thock(n: int, rng: np.random.Generator, snap: bool) -> np.ndarray:
    """A block landing: a stepped thump falling 140 -> 90 Hz, a soft noise tick, maybe a snap click."""
    t = np.arange(n) / SR
    out = tri16(np.cumsum(90 + 50 * np.exp(-t / 0.02)) / SR) * np.exp(-t / 0.035)
    out += 0.6 * lowpass(rng.uniform(-1, 1, n), 3000) * np.exp(-t / 0.006)
    if snap:
        click = np.diff(rng.uniform(-1, 1, n + 1)) * np.exp(-t / 0.0012)
        out += 0.25 * click
    return out


def pan(x: np.ndarray, p: float) -> np.ndarray:
    a = (p + 1) * math.pi / 4
    return np.stack([x * math.cos(a), x * math.sin(a)])


def delayed(x: np.ndarray, seconds: float) -> np.ndarray:
    lag = int(seconds * SR)
    return np.concatenate([np.zeros(lag), x[:-lag]])


def cue(rising: bool, seconds: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    steps = [C5, G5, C6] if rising else [C6, G5, C5]
    first = 0.02 if rising else 0.0

    lead = np.zeros(n)
    sparkle = np.zeros(n)
    for k, root in enumerate(steps):
        at = first + k * STEP
        last = k == len(steps) - 1
        add_note(lead, at, root, tri16, 0.08 if last else 0.05, bend=k == 0, vibrato=last)
        chord = [2 * root, 2.5 * root, 3 * root]  # octave-up major triad
        for j, freq in enumerate(chord if rising else chord[::-1]):
            add_note(sparkle, at + j * 0.014, freq, pulse, 0.02)
    sparkle = lowpass(sparkle, 5000)

    tones = pan(0.55 * lead, 0.0) + pan(0.16 * sparkle, -0.35)
    mono = 0.55 * lead + 0.16 * sparkle
    tones += pan(0.32 * delayed(mono, 0.075), 0.5) + pan(0.12 * delayed(mono, 0.15), -0.5)

    mix = tones.copy()
    land = 0.0 if rising else first + len(steps) * STEP - 0.005
    block = thock(int(0.16 * SR), rng, snap=True)
    at = int(land * SR)
    mix[:, at : at + len(block)] += 0.9 * block[: n - at]
    if not rising:  # the block settles with a small bounce
        bounce = thock(int(0.12 * SR), rng, snap=False)
        at += int(0.045 * SR)
        mix[:, at : at + len(bounce)] += 0.3 * bounce[: n - at]

    mix = np.stack([lowpass(side, 9000) for side in mix])
    fade = int(0.03 * SR)
    mix[:, -fade:] *= np.linspace(1, 0, fade)
    return mix * PEAK / np.max(np.abs(mix))


def write(name: str, stereo: np.ndarray) -> None:
    pcm = np.round(stereo.T * 32767).astype("<i2")
    with wave.open(str(SOUNDS / name), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes(pcm.tobytes())


if __name__ == "__main__":
    write("start.wav", cue(rising=True, seconds=0.42, seed=3))
    write("stop.wav", cue(rising=False, seconds=0.46, seed=5))
