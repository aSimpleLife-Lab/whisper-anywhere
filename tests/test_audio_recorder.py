from __future__ import annotations

import tempfile
from types import SimpleNamespace

import numpy as np
import pytest

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


def test_one_failed_reinitialisation_does_not_leave_the_microphone_dead(monkeypatch) -> None:
    # sounddevice counts only a successful Pa_Initialize, and Pa_Terminate on
    # a PortAudio that is down raises: one failed re-read made every later
    # rescan raise, and dictation stayed dead with no message (AU-8).
    PortAudioError = audio_module.sd.PortAudioError
    portaudio = {"up": True, "init_fails": 1}

    def terminate() -> None:
        if not portaudio["up"]:
            raise PortAudioError("Error terminating PortAudio: PortAudio not initialized")
        portaudio["up"] = False

    def initialize() -> None:
        if portaudio["init_fails"]:
            portaudio["init_fails"] -= 1
            raise PortAudioError("Error initializing PortAudio: Unanticipated host error")
        portaudio["up"] = True

    def query_devices():
        if not portaudio["up"]:
            raise PortAudioError("PortAudio not initialized")
        return [{"name": "Yeti", "max_input_channels": 1}]

    monkeypatch.setattr(audio_module.sd, "_terminate", terminate)
    monkeypatch.setattr(audio_module.sd, "_initialize", initialize)
    monkeypatch.setattr(audio_module.sd, "query_devices", query_devices)
    recorder = audio_module.AudioRecorder()

    recorder.rescan()  # fails inside, raises nothing
    assert portaudio["up"] is False
    names = [device.name for device in recorder.list_input_devices()]

    assert names == ["Default system microphone", "Yeti"]  # the next list tried again
    assert portaudio["up"] is True


def test_a_device_change_rereads_the_list_before_the_next_list_or_start(monkeypatch) -> None:
    # PortAudio's device list is the one from startup, while headsets and
    # controllers come and go and Windows renumbers the rest (AU-3).
    calls: list[str] = []
    monkeypatch.setattr(audio_module.sd, "_terminate", lambda: calls.append("terminate"))
    monkeypatch.setattr(audio_module.sd, "_initialize", lambda: calls.append("initialize"))
    monkeypatch.setattr(
        audio_module.sd, "query_devices", lambda: [{"name": "Yeti", "max_input_channels": 1}]
    )
    monkeypatch.setattr(audio_module.sd, "InputStream", FakeStream)
    recorder = audio_module.AudioRecorder()

    recorder.list_input_devices()
    assert calls == []  # nothing changed: nothing re-read
    recorder.mark_devices_changed()  # WM_DEVICECHANGE
    recorder.list_input_devices()
    recorder.list_input_devices()
    assert calls == ["terminate", "initialize"]

    recorder.start("default")
    recorder.mark_devices_changed()
    recorder.list_input_devices()  # never under an open stream
    assert calls == ["terminate", "initialize"]
    recorder.cancel()
    recorder.start("default")  # the next start re-reads first
    assert calls == ["terminate", "initialize"] * 2
    recorder.cancel()


def test_a_saved_microphone_is_opened_by_its_name_when_windows_renumbers_them(
    monkeypatch,
) -> None:
    # The saved "Yeti at 1" opened whichever microphone took its number, while
    # the window still said Yeti (AU-3).
    yeti, pro_x, dualsense = (
        {"name": name, "max_input_channels": 1} for name in ("Yeti", "PRO X 2", "DualSense")
    )
    portaudio = {"windows": [yeti, pro_x], "list": [yeti, pro_x]}  # PortAudio's copy
    opened: list[int | None] = []

    class OpeningStream(FakeStream):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            opened.append(kwargs["device"])

    monkeypatch.setattr(audio_module.sd, "_terminate", lambda: None)
    monkeypatch.setattr(
        audio_module.sd, "_initialize", lambda: portaudio.update(list=list(portaudio["windows"]))
    )
    monkeypatch.setattr(audio_module.sd, "query_devices", lambda: portaudio["list"])
    monkeypatch.setattr(audio_module.sd, "InputStream", OpeningStream)
    recorder = audio_module.AudioRecorder()

    portaudio["windows"] = [dualsense, yeti, pro_x]  # the controller connects
    recorder.mark_devices_changed()
    recorder.start("0", microphone_name="Yeti")
    recorder.cancel()
    # Saved in an earlier session: the id is someone else's in this list.
    recorder.start("2", microphone_name="Yeti")
    recorder.cancel()
    portaudio["windows"] = [dualsense, pro_x]  # the Yeti is unplugged
    recorder.mark_devices_changed()
    with pytest.raises(audio_module.AudioRecorderError):
        recorder.start("1", microphone_name="Yeti")

    assert opened == [1, 1]
    assert not recorder.is_recording


def test_a_quick_tap_returns_no_audio_instead_of_an_error(monkeypatch) -> None:
    # MME delivers its first buffer some time after start(): a quick tap of
    # the shortcut showed "Error: No microphone audio was captured." (AU-5).
    clock = [100.0]
    monkeypatch.setattr(audio_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(audio_module.sd, "InputStream", FakeStream)
    recorder = audio_module.AudioRecorder()

    recorder.start("default")
    clock[0] += 0.15
    audio = recorder.stop()
    assert audio.dtype == np.float32 and audio.size == 0

    recorder.start("default")
    clock[0] += 2.0  # two seconds with no audio at all is a real fault
    with pytest.raises(audio_module.AudioRecorderError, match="No microphone audio"):
        recorder.stop()


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
    # v0.1 wrote recordings to %TEMP%, not the working folder: watch both.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
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
