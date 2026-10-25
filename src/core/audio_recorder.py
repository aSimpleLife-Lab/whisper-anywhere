from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
CHANNELS = 1


class AudioRecorderError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioDevice:
    id: str
    name: str


@dataclass(frozen=True)
class _Input:
    id: str
    name: str
    hostapi: int | None


class AudioRecorder:
    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._level_callback: Callable[[float], None] | None = None
        self._last_status = ""
        self.is_recording = False

    def list_input_devices(self, keep: str | None = None) -> list[AudioDevice]:
        """Microphones on the default audio API, each listed once.

        Windows exposes every microphone through MME, DirectSound, WASAPI and
        WDM-KS, so the raw list shows each one up to four times. `keep` still
        lists a saved device that lives on another API.
        """
        devices = [AudioDevice("default", "Default system microphone")]
        try:
            default_api = sd.default.hostapi
        except Exception:
            default_api = None
        for device in self._all_inputs():
            on_default_api = device.hostapi in (None, default_api)
            if (on_default_api and "sound mapper" not in device.name.lower()) or device.id == keep:
                devices.append(AudioDevice(device.id, device.name))
        return devices

    def input_device_by_id(self, device_id: str | None) -> AudioDevice | None:
        wanted = str(device_id or "default")
        if wanted == "default":
            return AudioDevice("default", "Default system microphone")
        for device in self._all_inputs():
            if device.id == wanted:
                return AudioDevice(device.id, device.name)
        return None

    def _all_inputs(self) -> list["_Input"]:
        try:
            raw = sd.query_devices()
        except Exception:
            return []
        inputs = []
        for index, device in enumerate(raw):
            if int(device.get("max_input_channels", 0)) > 0:
                inputs.append(
                    _Input(
                        str(index),
                        str(device.get("name", f"Microphone {index}")),
                        device.get("hostapi"),
                    )
                )
        return inputs

    def start(
        self,
        microphone_device: str = "default",
        level_callback: Callable[[float], None] | None = None,
    ) -> None:
        if self.is_recording:
            raise AudioRecorderError("The microphone is already recording.")

        self._frames = []
        self._last_status = ""
        self._level_callback = level_callback
        device = (
            None
            if microphone_device in ("", "default", None)
            else int(microphone_device)
        )

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

    def stop(self) -> np.ndarray:
        """Stop recording and return mono 16 kHz float32 samples in memory.

        Audio never touches the disk, so there is no temporary recording to
        clean up and nothing left behind if the app is killed.
        """
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

        audio = np.concatenate(frames, axis=0).reshape(-1)
        if audio.size == 0:
            raise AudioRecorderError("No microphone audio was captured.")
        return audio.astype(np.float32) / 32768.0

    def cancel(self) -> None:
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
            self._frames = []
        self._level_callback = None

    def close(self) -> None:
        self.cancel()

    def _on_audio(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            self._last_status = str(status)

        chunk = indata.copy()
        with self._lock:
            self._frames.append(chunk)

        callback = self._level_callback
        if callback is not None and chunk.size:
            rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float32)))) / 32768.0
            callback(max(0.0, min(1.0, rms * 8.0)))
