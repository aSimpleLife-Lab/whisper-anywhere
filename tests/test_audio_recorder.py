from __future__ import annotations

from pathlib import Path
import wave

import numpy as np

import core.audio_recorder as audio_module


class FakeStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def test_start_and_audio_callback_manage_buffers_and_levels(monkeypatch) -> None:
    created = {}

    class FakeInputStream(FakeStream):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            created.update(kwargs)

    monkeypatch.setattr(audio_module.sd, "InputStream", FakeInputStream)
    recorder = audio_module.AudioRecorder()
    levels: list[float] = []

    recorder.start("2", level_callback=levels.append)
    recorder._on_audio(
        np.array([[1200], [-1200]], dtype=np.int16), 2, None, "input overflow"
    )

    assert recorder.is_recording is True
    assert created["device"] == 2
    assert len(recorder._frames) == 1
    assert recorder._last_status == "input overflow"
    assert 0.0 < levels[-1] <= 1.0

    recorder.cancel()


def test_list_input_devices_returns_structured_microphone_entries(monkeypatch) -> None:
    monkeypatch.setattr(
        audio_module.sd,
        "query_devices",
        lambda: [
            {"name": "Speaker", "max_input_channels": 0},
            {"name": "USB Mic", "max_input_channels": 2},
            {"name": "Headset Mic", "max_input_channels": 1},
        ],
    )

    devices = audio_module.AudioRecorder().list_input_devices()

    assert devices == [
        audio_module.AudioDevice("default", "Default system microphone"),
        audio_module.AudioDevice("1", "USB Mic"),
        audio_module.AudioDevice("2", "Headset Mic"),
    ]


def test_list_input_devices_handles_no_microphone_query_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        audio_module.sd,
        "query_devices",
        lambda: (_ for _ in ()).throw(RuntimeError("no device")),
    )

    devices = audio_module.AudioRecorder().list_input_devices()

    assert devices == [audio_module.AudioDevice("default", "Default system microphone")]


def test_stop_writes_wav_with_expected_format(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audio_module.tempfile, "gettempdir", lambda: str(tmp_path))

    recorder = audio_module.AudioRecorder()
    recorder.is_recording = True
    recorder._stream = FakeStream()
    recorder._frames = [
        np.array([[1000], [-1000]], dtype=np.int16),
        np.array([[500], [0]], dtype=np.int16),
    ]

    output_path = Path(recorder.stop())

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() == audio_module.CHANNELS
        assert wav_file.getsampwidth() == audio_module.SAMPLE_WIDTH_BYTES
        assert wav_file.getframerate() == audio_module.SAMPLE_RATE
        assert wav_file.getnframes() == 4

    assert recorder._frames == []
    assert output_path.unlink() is None


def test_stop_output_file_is_immediately_removable_after_creation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(audio_module.tempfile, "gettempdir", lambda: str(tmp_path))

    recorder = audio_module.AudioRecorder()
    recorder.is_recording = True
    recorder._stream = FakeStream()
    recorder._frames = [np.array([[250], [-250]], dtype=np.int16)]

    output_path = Path(recorder.stop())

    assert output_path.exists()
    output_path.unlink()


def test_cancel_cleans_up_after_simulated_device_disconnect() -> None:
    class BrokenStream:
        def stop(self) -> None:
            raise RuntimeError("device disconnected")

        def close(self) -> None:
            raise RuntimeError("device disconnected")

    recorder = audio_module.AudioRecorder()
    recorder.is_recording = True
    recorder._stream = BrokenStream()
    recorder._frames = [np.array([[1]], dtype=np.int16)]
    recorder._level_callback = lambda level: None

    recorder.cancel()

    assert recorder.is_recording is False
    assert recorder._stream is None
    assert recorder._frames == []
    assert recorder._level_callback is None
