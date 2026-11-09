from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
CHANNELS = 1
# MME delivers its first buffer a moment after the stream starts: a tap
# shorter than this is too short to be speech, not a broken microphone.
BRIEF_SECONDS = 0.3
# Loading Voz holds the GIL for up to 0.73 s (Ryzen 9 5950X), and _on_audio
# needs it. MME's default 0.18 s of queued buffers would drop what was said
# meanwhile, so a recording a load runs during queues 3 s.
LOAD_HEADROOM_SECONDS = 3.0
CATCH_UP_SECONDS = 2.0  # longer than any GIL hold still left when stop() runs


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
        self._started_at = 0.0
        self._devices_changed = False
        self._audio_flowing = threading.Event()
        self._headroom = False
        self._received = 0  # frames handed to _on_audio
        self._origin = math.inf  # when the first of them was said, monotonic
        self.is_recording = False

    def mark_devices_changed(self) -> None:
        """Windows reported a device change (WM_DEVICECHANGE): the next list
        or start re-reads the devices first."""
        self._devices_changed = True

    def list_input_devices(self, keep: str | None = None) -> list[AudioDevice]:
        """Microphones on the default audio API, each listed once.

        Windows exposes every microphone through MME, DirectSound, WASAPI and
        WDM-KS, so the raw list shows each one up to four times. `keep` still
        lists a saved device that lives on another API.
        """
        devices = [AudioDevice("default", "Default system microphone")]
        inputs = self._all_inputs()  # re-reads a changed list first
        try:
            default_api = sd.default.hostapi
        except Exception:
            default_api = None
        for device in inputs:
            on_default_api = device.hostapi in (None, default_api)
            if (on_default_api and "sound mapper" not in device.name.lower()) or device.id == keep:
                devices.append(AudioDevice(device.id, device.name))
        return devices

    def rescan(self) -> None:
        """Re-read the devices. PortAudio keeps the list from startup, so a
        headset connected later never showed up; never done under an open
        stream, which it would break. About 0.1 s.

        Never raises: a failed re-read leaves PortAudio down, and the next
        list or start tries again."""
        if self._stream is not None:
            return
        try:
            sd._terminate()
        except Exception:
            pass  # already down: the last re-read failed
        try:
            sd._initialize()
        except Exception:
            self._devices_changed = True
        else:
            self._devices_changed = False

    def input_device_by_id(self, device_id: str | None) -> AudioDevice | None:
        wanted = str(device_id or "default")
        if wanted == "default":
            return AudioDevice("default", "Default system microphone")
        for device in self._all_inputs():
            if device.id == wanted:
                return AudioDevice(device.id, device.name)
        return None

    def _all_inputs(self) -> list["_Input"]:
        if self._devices_changed:
            self.rescan()
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
        microphone_name: str = "",
        headroom: bool = False,
    ) -> None:
        """Record from `microphone_device`. With `microphone_name`, only from
        the microphone of that name, wherever Windows has moved it. With
        `headroom`, the recording outlasts a model load holding the GIL."""
        if self.is_recording:
            raise AudioRecorderError("The microphone is already recording.")
        if self._devices_changed:
            self.rescan()
        device_id = str(microphone_device or "default")
        if device_id != "default" and microphone_name:
            device_id = self._device_named(device_id, microphone_name)

        self._frames = []
        self._last_status = ""
        self._level_callback = level_callback
        self._audio_flowing.clear()
        self._headroom = headroom
        self._received = 0
        self._origin = math.inf
        device = None if device_id == "default" else int(device_id)

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                device=device,
                latency=LOAD_HEADROOM_SECONDS if headroom else None,  # None: sounddevice's default
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioRecorderError(
                "Could not start the microphone. Check that a microphone is connected and not blocked by Windows privacy settings."
            ) from exc

        self._started_at = time.monotonic()
        self.is_recording = True

    def wait_for_audio(self, timeout: float) -> bool:
        """True once the first buffer is in, or False after `timeout` seconds."""
        return self._audio_flowing.wait(timeout)

    def _device_named(self, device_id: str, name: str) -> str:
        """The id the saved microphone has now. Windows renumbers microphones
        as they come and go, so a saved id can open another one."""
        device = self.input_device_by_id(device_id)
        if device is not None and device.name == name:
            return device_id
        self.rescan()
        for device in self.list_input_devices():
            if device.id != "default" and device.name == name:
                return device.id
        raise AudioRecorderError(f"The saved microphone ({name}) is not connected.")

    def stop(self) -> np.ndarray:
        """Stop recording and return mono 16 kHz float32 samples in memory.

        Audio never touches the disk, so there is no temporary recording to
        clean up and nothing left behind if the app is killed. A quick tap
        can return no samples at all.
        """
        if not self.is_recording:
            raise AudioRecorderError("The microphone is not recording.")

        lasted = time.monotonic() - self._started_at
        if self._headroom:
            self._catch_up()
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

        audio = np.concatenate(frames, axis=0).reshape(-1) if frames else np.zeros(0, np.int16)
        if audio.size == 0 and lasted >= BRIEF_SECONDS:
            raise AudioRecorderError("No microphone audio was captured.")
        # A quick tap ends before the first buffer: no samples, and no error.
        return audio.astype(np.float32) / 32768.0

    def _catch_up(self) -> None:
        """Wait until everything said before now is in. While a model load
        holds the GIL, _on_audio waits and MME queues the buffers, and
        stopping an input stream drops the ones it has not handed over.

        ponytail: the release reaches this process late if it came during
        the load, so up to that hold's length of later audio comes along."""
        due = (time.monotonic() - self._origin) * SAMPLE_RATE
        deadline = time.monotonic() + CATCH_UP_SECONDS
        while self._received < due and time.monotonic() < deadline:
            time.sleep(0.01)

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

        now = time.monotonic()
        chunk = indata.copy()
        with self._lock:
            self._frames.append(chunk)
            self._received += frames
            # The buffer handed over soonest after it filled dates the first
            # sample best: one stalled behind the GIL only arrives late.
            self._origin = min(self._origin, now - self._received / SAMPLE_RATE)
        self._audio_flowing.set()

        callback = self._level_callback
        if callback is not None and chunk.size:
            rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float32)))) / 32768.0
            callback(max(0.0, min(1.0, rms * 8.0)))
