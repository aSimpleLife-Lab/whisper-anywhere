from __future__ import annotations

from types import SimpleNamespace

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


def test_rescan_rereads_devices_but_never_under_an_open_stream(monkeypatch) -> None:
    # PortAudio keeps its startup device list, so a headset connected later
    # never appeared; re-reading it under a live stream would break the stream.
    calls: list[str] = []
    monkeypatch.setattr(audio_module.sd, "_terminate", lambda: calls.append("terminate"))
    monkeypatch.setattr(audio_module.sd, "_initialize", lambda: calls.append("initialize"))
    recorder = audio_module.AudioRecorder()

    recorder.rescan()
    recorder._stream = FakeStream()
    recorder.rescan()

    assert calls == ["terminate", "initialize"]


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


def test_list_input_devices_shows_each_microphone_once(monkeypatch) -> None:
    monkeypatch.setattr(audio_module.sd, "default", SimpleNamespace(hostapi=0))
    monkeypatch.setattr(
        audio_module.sd,
        "query_devices",
        lambda: [
            {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
            {"name": "Yeti", "max_input_channels": 2, "hostapi": 0},
            {"name": "Yeti", "max_input_channels": 2, "hostapi": 1},
            {"name": "Yeti", "max_input_channels": 2, "hostapi": 2},
        ],
    )
    recorder = audio_module.AudioRecorder()

    assert recorder.list_input_devices() == [
        audio_module.AudioDevice("default", "Default system microphone"),
        audio_module.AudioDevice("1", "Yeti"),
    ]
    # A device saved from another audio API stays selectable.
    assert recorder.list_input_devices(keep="3")[-1] == audio_module.AudioDevice("3", "Yeti")
    assert recorder.input_device_by_id("2") == audio_module.AudioDevice("2", "Yeti")


def test_list_input_devices_handles_no_microphone_query_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        audio_module.sd,
        "query_devices",
        lambda: (_ for _ in ()).throw(RuntimeError("no device")),
    )

    devices = audio_module.AudioRecorder().list_input_devices()

    assert devices == [audio_module.AudioDevice("default", "Default system microphone")]


def test_stop_returns_float_samples_without_touching_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    recorder = audio_module.AudioRecorder()
    recorder.is_recording = True
    recorder._stream = FakeStream()
    recorder._frames = [
        np.array([[16384], [-16384]], dtype=np.int16),
        np.array([[8192], [0]], dtype=np.int16),
    ]

    audio = recorder.stop()

    assert audio.dtype == np.float32
    assert audio.shape == (4,)
    assert np.allclose(audio, [0.5, -0.5, 0.25, 0.0])
    assert recorder._frames == []
    assert recorder.is_recording is False
    assert list(tmp_path.iterdir()) == []


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
