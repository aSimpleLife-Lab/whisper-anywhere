from __future__ import annotations

import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

from core.settings_manager import APP_NAME

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


class AudioRecorderError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioDevice:
    id: str
    name: str


class AudioRecorder:
    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._level_callback: Callable[[float], None] | None = None
        self._last_status = ""
        self.is_recording = False

    def list_input_devices(self) -> list[AudioDevice]:
        devices = [AudioDevice("default", "Default system microphone")]
        try:
            for index, device in enumerate(sd.query_devices()):
                if int(device.get("max_input_channels", 0)) > 0:
                    name = str(device.get("name", f"Microphone {index}"))
                    devices.append(AudioDevice(str(index), name))
        except Exception:
            return devices
        return devices

    def start(self, microphone_device: str = "default", level_callback: Callable[[float], None] | None = None) -> None:
        if self.is_recording:
            raise AudioRecorderError("The microphone is already recording.")

        self._frames = []
        self._last_status = ""
        self._level_callback = level_callback
        device = None if microphone_device in ("", "default", None) else int(microphone_device)

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                device=device,
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioRecorderError(
                "Could not start the microphone. Check that a microphone is connected and not blocked by Windows privacy settings."
            ) from exc

        self.is_recording = True

    def stop(self) -> str:
        if not self.is_recording:
            raise AudioRecorderError("The microphone is not recording.")

        stream = self._stream
        self._stream = None
        self.is_recording = False

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        with self._lock:
            frames = [frame.copy() for frame in self._frames]
            self._frames = []

        if not frames:
            raise AudioRecorderError("No microphone audio was captured.")

        audio = np.concatenate(frames, axis=0)
        if audio.size == 0:
            raise AudioRecorderError("No microphone audio was captured.")

        temp_dir = Path(tempfile.gettempdir()) / APP_NAME
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".wav", prefix="recording-", dir=temp_dir, delete=False) as temp_file:
            output_path = temp_file.name

        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(audio.astype(np.int16).tobytes())

        return output_path

    def close(self) -> None:
        if self.is_recording:
            try:
                self.stop()
            except Exception:
                pass
        elif self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _on_audio(self, indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
        if status:
            self._last_status = str(status)

        chunk = indata.copy()
        with self._lock:
            self._frames.append(chunk)

        if self._level_callback is not None:
            normalized = chunk.astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(np.square(normalized)))) if normalized.size else 0.0
            self._level_callback(max(0.0, min(1.0, rms * 8.0)))
