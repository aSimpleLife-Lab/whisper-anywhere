from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from core.settings_manager import DEFAULT_SETTINGS
from core.model_manager import MODEL_INFO
from core.transcriber import (
    RuntimeOptions,
    Transcriber,
    default_cpu_threads,
)

SPEECH = np.full(16000, 0.1, np.float32)  # 1 s, well above the silence floor


def install_fake_voz(monkeypatch: pytest.MonkeyPatch, events: list[str]):
    from core import voz_engine

    class FakeVoz:
        def __init__(self, model_root, cpu_threads=None) -> None:
            events.append("load")

        def warm_up(self) -> None:
            events.append("warm-up")

        def transcribe(self, samples) -> str:
            events.append("transcribe")
            return "hello voz"

        def __del__(self) -> None:  # the ONNX Runtime sessions go with it
            events.append("free")

    monkeypatch.setattr(voz_engine, "missing_files", lambda root: [])
    monkeypatch.setattr(voz_engine, "VozModel", FakeVoz)
    return FakeVoz


def test_a_new_voz_load_frees_the_old_model_first(monkeypatch, tmp_path) -> None:
    # Freed after the new load, the old sessions' mapped cache files blocked
    # a cache rebuild.
    events: list[str] = []
    install_fake_voz(monkeypatch, events)
    transcriber = Transcriber(FakeModelManager("voz"))
    transcriber._load_voz_model(make_settings(tmp_path, cpu_threads=4), warm_up=False)
    events.clear()

    transcriber._load_voz_model(make_settings(tmp_path, cpu_threads=8), warm_up=False)

    assert events == ["free", "load"]


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    """Every engine is a fake here: nothing may reach huggingface.co."""

    def refuse(*args, **kwargs):
        pytest.fail("a test tried to download a model")

    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=refuse)
    )


class FakeModelManager:
    def __init__(self, selected_model: str = "base", ready: tuple[str, ...] = ()) -> None:
        self._selected_model = selected_model
        self.ready = set(ready)  # models with a marker file
        self.marked: list[str] = []
        self.mark_error: OSError | None = None

    @property
    def ready_marks(self) -> int:
        return len(self.marked)

    def is_model_ready(self, model_name: str | None = None) -> bool:
        return (model_name or self._selected_model) in self.ready

    def mark_model_ready(self, model_name: str | None = None) -> None:
        if self.mark_error is not None:
            raise self.mark_error
        self.marked.append(model_name or self._selected_model)
        self.ready.add(model_name or self._selected_model)

    def selected_model(self) -> str:
        return self._selected_model

    def backend_model_name(self, model_name: str | None = None) -> str:
        return MODEL_INFO[model_name or self._selected_model].backend_name

    def selected_info(self):
        return MODEL_INFO[self._selected_model]

    def model_engine(self, model_name: str | None = None) -> str:
        selected = model_name or self._selected_model
        return MODEL_INFO[selected].engine


def make_settings(tmp_path, **overrides):
    settings = dict(DEFAULT_SETTINGS)
    settings.update(
        {
            "model_path": str(tmp_path / "models"),
            "device": "auto",
            "use_gpu_if_available": False,
            "fallback_to_cpu": True,
            "compute_type": "auto",
            "performance_preset": "balanced",
            "low_vram_mode": False,
            "cpu_threads": "auto",
            "unload_model_after": "never",
            "language_mode": "auto",
            "forced_language": "",
            "translate_to_english": False,
        }
    )
    settings.update(overrides)
    return settings


def test_voz_loads_then_warms_up(monkeypatch, tmp_path) -> None:
    from core import voz_engine

    events: list[str] = []

    class FakeVoz:
        def __init__(self, *args, **kwargs) -> None:
            events.append("load")

        def warm_up(self) -> None:
            events.append("warm-up")

    monkeypatch.setattr(voz_engine, "missing_files", lambda root: [])
    monkeypatch.setattr(voz_engine, "VozModel", FakeVoz)
    transcriber = Transcriber(FakeModelManager())
    transcriber._load_voz_model(make_settings(tmp_path), warm_up=True)
    assert events == ["load", "warm-up"]


def test_each_frees_the_model_after_every_dictation(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    install_fake_voz(monkeypatch, events)
    transcriber = Transcriber(FakeModelManager("voz"))

    text = transcriber.transcribe(SPEECH, make_settings(tmp_path, unload_model_after="each"))

    assert text == "hello voz"
    assert not transcriber.is_loaded()
    assert events == ["load", "transcribe", "free"]


@pytest.mark.parametrize("after", ["never", "5", "15", "60", None])
def test_only_each_unloads_after_a_dictation(monkeypatch, tmp_path, after) -> None:
    # The idle minutes are counted by the window's timer, which calls unload().
    events: list[str] = []
    install_fake_voz(monkeypatch, events)
    transcriber = Transcriber(FakeModelManager("voz"))
    settings = make_settings(tmp_path, unload_model_after=after)
    if after is None:  # saved before the setting existed
        del settings["unload_model_after"]

    transcriber.transcribe(SPEECH, settings)
    transcriber.transcribe(SPEECH, settings)

    assert transcriber.is_loaded()
    assert events.count("load") == 1


def test_low_ram_mode_no_longer_unloads_or_forces_the_cpu(monkeypatch, tmp_path) -> None:
    # It silently won over "Keep the model loaded" and pinned Whisper and
    # FunASR to the CPU; "unload_model_after" replaces both switches.
    events: list[str] = []
    install_fake_voz(monkeypatch, events)
    transcriber = Transcriber(FakeModelManager("voz"))
    monkeypatch.setattr(transcriber, "_cuda_available", lambda: True)
    monkeypatch.setattr(transcriber, "_torch_cuda_available", lambda: True)
    settings = make_settings(
        tmp_path,
        low_ram_mode=True,
        keep_model_loaded=False,
        device="gpu",
        use_gpu_if_available=True,
    )

    transcriber.transcribe(SPEECH, settings)

    assert transcriber.is_loaded()
    assert transcriber._resolve_device(settings, []) == "cuda"
    assert transcriber._resolve_funasr_device(settings, []) == "cuda"


def test_unload_waits_for_a_running_dictation(monkeypatch, tmp_path) -> None:
    # The window's idle timer must never pull the model from under a dictation.
    events: list[str] = []
    fake_voz = install_fake_voz(monkeypatch, events)
    running, finish = threading.Event(), threading.Event()

    def slow_transcribe(self, samples) -> str:
        running.set()
        assert finish.wait(5)
        return "hello voz"

    monkeypatch.setattr(fake_voz, "transcribe", slow_transcribe)
    transcriber = Transcriber(FakeModelManager("voz"))
    dictation = threading.Thread(
        target=transcriber.transcribe, args=(SPEECH, make_settings(tmp_path))
    )
    dictation.start()
    assert running.wait(5)

    idle_timeout = threading.Thread(target=transcriber.unload)
    idle_timeout.start()
    idle_timeout.join(0.3)
    assert idle_timeout.is_alive() and transcriber.is_loaded()

    finish.set()
    dictation.join(5)
    idle_timeout.join(5)
    assert not transcriber.is_loaded()
    assert events == ["load", "free"]

    transcriber.unload()  # nothing loaded: nothing happens
    assert events == ["load", "free"]


def test_only_the_background_warm_up_runs_the_warm_up_pass(monkeypatch, tmp_path) -> None:
    # A dictation's own clip warms the model up: the extra pass cost
    # 0.09-0.16 s on every reload.
    events: list[str] = []
    install_fake_voz(monkeypatch, events)
    settings = make_settings(tmp_path)
    dictation = Transcriber(FakeModelManager("voz"))
    background = Transcriber(FakeModelManager("voz"))

    dictation.transcribe(SPEECH, settings)
    background.prepare_selected_model(settings)

    assert events == ["load", "transcribe", "load", "warm-up"]


def install_fake_whisper(
    monkeypatch: pytest.MonkeyPatch, whisper_model_class, events: list[str] | None = None
) -> list[tuple]:
    """faster-whisper whose download never leaves the PC: records
    (model, cache_dir, local_files_only)."""
    downloads: list[tuple] = []

    def download_model(size_or_id, cache_dir=None, local_files_only=False):
        downloads.append((size_or_id, cache_dir, local_files_only))
        return str(Path(cache_dir) / f"whisper-{size_or_id}")

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper_model_class)
    )
    monkeypatch.setitem(
        sys.modules, "faster_whisper.utils", SimpleNamespace(download_model=download_model)
    )
    return downloads


def install_fake_funasr(
    monkeypatch: pytest.MonkeyPatch, automodel_class, events: list[str] | None = None
) -> list[tuple]:
    """FunASR whose Hugging Face downloads never leave the PC: records
    (repo, cache_dir, local_files_only)."""
    snapshots: list[tuple] = []

    def snapshot_download(repo_id, cache_dir=None, local_files_only=False):
        snapshots.append((repo_id, cache_dir, local_files_only))
        return str(Path(cache_dir) / repo_id.replace("/", "--"))

    monkeypatch.setitem(
        sys.modules, "funasr", SimpleNamespace(AutoModel=automodel_class)
    )
    monkeypatch.setitem(
        sys.modules,
        "funasr.download.name_maps_from_hub",
        SimpleNamespace(name_maps_hf={"fsmn-vad": "funasr/fsmn-vad"}),
    )
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download)
    )
    return snapshots


class FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class SpeakingWhisper:
    def __init__(self, model_dir: str, **kwargs) -> None:
        self.model_dir = model_dir
        self.kwargs = dict(kwargs)

    def transcribe(self, audio, **kwargs):
        return [FakeSegment("hello")], {"language": "en"}


def test_whisper_downloads_first_and_loads_its_folder_offline(monkeypatch, tmp_path) -> None:
    # The first download ran inside the load with no timeout, and a stalled
    # proxy hung it (SW-1).
    events: list[str] = []

    class LocalWhisper(SpeakingWhisper):
        def __init__(self, model_dir: str, **kwargs) -> None:
            events.append(f"load {model_dir} offline={kwargs.get('local_files_only')}")
            super().__init__(model_dir, **kwargs)

    downloads = install_fake_whisper(monkeypatch, LocalWhisper, events)
    transcriber = Transcriber(FakeModelManager("small"))

    assert transcriber.transcribe(SPEECH, make_settings(tmp_path, device="cpu")) == "hello"

    models = tmp_path / "models"
    assert downloads == [("small", str(models), False)]
    assert events == [f"load {models / 'whisper-small'} offline=True"]


def test_a_first_whisper_download_trusts_the_windows_certificate_store(monkeypatch, tmp_path) -> None:
    # Voz and FunASR downloads used the Windows store; Whisper's first download
    # failed where Python's own bundle lacks the PC's root certificate (AU-6).
    order: list[str] = []
    install_fake_whisper(monkeypatch, SpeakingWhisper)
    real_download = sys.modules["faster_whisper.utils"].download_model
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper.utils",
        SimpleNamespace(download_model=lambda *args, **kwargs: order.append("download") or real_download(*args, **kwargs)),
    )
    transcriber = Transcriber(FakeModelManager("small"))
    monkeypatch.setattr(transcriber, "_enable_system_certificate_store", lambda: order.append("store"))

    transcriber.transcribe(SPEECH, make_settings(tmp_path, device="cpu"))

    assert order == ["store", "download"]


def test_a_ready_whisper_model_never_asks_huggingface(monkeypatch, tmp_path) -> None:
    # Every load asked huggingface.co (no timeout) while the hooks were off.
    # A model whose cached copy was deleted downloads again.
    calls: list[bool] = []
    deleted = [False]

    def download_model(size_or_id, cache_dir=None, local_files_only=False):
        calls.append(local_files_only)
        if local_files_only and deleted[0]:
            raise FileNotFoundError("cached copy deleted")  # huggingface_hub's error
        return str(tmp_path / size_or_id)

    install_fake_whisper(monkeypatch, SpeakingWhisper)
    monkeypatch.setitem(
        sys.modules, "faster_whisper.utils", SimpleNamespace(download_model=download_model)
    )
    settings = make_settings(tmp_path, device="cpu")

    Transcriber(FakeModelManager("base")).transcribe(SPEECH, settings)
    Transcriber(FakeModelManager("base", ready=("base",))).transcribe(SPEECH, settings)
    deleted[0] = True
    Transcriber(FakeModelManager("base", ready=("base",))).transcribe(SPEECH, settings)

    assert calls == [False, True, True, False]  # not ready, ready, ready but gone


def test_funasr_downloads_into_the_model_folder_first(monkeypatch, tmp_path) -> None:
    # funasr asked huggingface.co inside the load, with no timeout, on every
    # load, and kept 1-2 GB in %USERPROFILE%\.cache (SW-1, SW-4).
    events: list[str] = []
    created: list[dict] = []

    class FakeFunASRModel:
        def __init__(self, **kwargs) -> None:
            events.append("load")
            created.append(kwargs)

        def generate(self, **kwargs):
            return [{"text": "hello"}]

    snapshots = install_fake_funasr(monkeypatch, FakeFunASRModel, events)
    transcriber = Transcriber(FakeModelManager("sensevoice-small"))
    settings = make_settings(tmp_path, device="cpu", unload_model_after="each")

    assert transcriber.transcribe(SPEECH, settings) == "hello"
    assert transcriber.transcribe(SPEECH, settings) == "hello"

    hub = tmp_path / "models" / "funasr" / "huggingface" / "hub"
    assert snapshots == [
        ("FunAudioLLM/SenseVoiceSmall", str(hub), False),
        ("funasr/fsmn-vad", str(hub), False),
        ("FunAudioLLM/SenseVoiceSmall", str(hub), True),  # ready now: offline
        ("funasr/fsmn-vad", str(hub), True),
    ]
    assert created[0]["model"] == str(hub / "FunAudioLLM--SenseVoiceSmall")
    assert created[0]["vad_model"] == str(hub / "funasr--fsmn-vad")
    assert events[:1] == ["load"]


def test_a_marker_that_cannot_be_written_never_costs_a_dictation(monkeypatch, tmp_path) -> None:
    # The marker was rewritten after every dictation, inside the result's error
    # handling: a locked or unwritable file threw the finished text away.
    downloads = install_fake_whisper(monkeypatch, SpeakingWhisper)
    manager = FakeModelManager("base")
    manager.mark_error = PermissionError(13, "Access is denied")
    transcriber = Transcriber(manager)
    settings = make_settings(tmp_path, device="cpu", unload_model_after="each")

    assert transcriber.transcribe(SPEECH, settings) == "hello"
    assert transcriber.transcribe(SPEECH, settings) == "hello"

    # Ready in memory after its first load: the reload still stays offline.
    assert [download[2] for download in downloads] == [False, True]


def test_the_ready_marker_is_written_once_and_never_for_voz(monkeypatch, tmp_path) -> None:
    # Voz is checked by its files and never reads the marker.
    install_fake_whisper(monkeypatch, SpeakingWhisper)
    install_fake_voz(monkeypatch, [])
    settings = make_settings(tmp_path, device="cpu", unload_model_after="each")
    whisper = FakeModelManager("base")
    voz = FakeModelManager("voz")
    installed = FakeModelManager("small", ready=("small",))

    for manager in (whisper, voz, installed):
        transcriber = Transcriber(manager)
        for _ in range(3):
            transcriber.transcribe(SPEECH, settings)
        transcriber.prepare_selected_model(settings)

    assert whisper.marked == ["base"]
    assert voz.marked == []
    assert installed.marked == []  # its marker is there already


def test_a_model_picked_mid_operation_does_not_change_what_loads_or_is_marked(
    monkeypatch, tmp_path
) -> None:
    # Picking a model during a transcription or download marked the new one
    # installed, and its first dictation then downloaded it (UA-2, AU-7).
    manager = FakeModelManager("base")

    class PickedWhileLoading(SpeakingWhisper):
        def __init__(self, model_dir: str, **kwargs) -> None:
            manager._selected_model = "small"  # setup or a settings import lands now
            super().__init__(model_dir, **kwargs)

    downloads = install_fake_whisper(monkeypatch, PickedWhileLoading)
    transcriber = Transcriber(manager)
    settings = make_settings(tmp_path, device="cpu", unload_model_after="each")

    assert transcriber.transcribe(SPEECH, settings) == "hello"
    transcriber.prepare_selected_model(settings, model_name="tiny")
    transcriber.transcribe(SPEECH, settings, model_name="medium")

    assert [download[0] for download in downloads] == ["base", "tiny", "medium"]
    assert manager.marked == ["base", "tiny", "medium"]


def test_the_whisper_cpu_fallback_reloads_the_model_the_dictation_began_with(
    monkeypatch, tmp_path
) -> None:
    manager = FakeModelManager("base")

    class GpuFailsAfterAPick(SpeakingWhisper):
        def transcribe(self, audio, **kwargs):
            if self.kwargs["device"] == "cuda":
                manager._selected_model = "small"
                raise RuntimeError("gpu transcription failed")
            return super().transcribe(audio, **kwargs)

    downloads = install_fake_whisper(monkeypatch, GpuFailsAfterAPick)
    transcriber = Transcriber(manager)
    monkeypatch.setattr(transcriber, "_cuda_available", lambda: True)
    monkeypatch.setattr(
        transcriber, "_supported_compute_types", lambda device: {"int8", "float16", "float32"}
    )

    text = transcriber.transcribe(
        SPEECH, make_settings(tmp_path, device="gpu", use_gpu_if_available=True)
    )

    assert text == "hello"
    assert [download[0] for download in downloads] == ["base", "base"]
    assert manager.marked == ["base"]


def test_funasr_follows_the_model_the_dictation_began_with(monkeypatch, tmp_path) -> None:
    manager = FakeModelManager("sensevoice-small")
    built: list[str] = []
    inputs: list[object] = []

    class FakeFunASRModel:
        def __init__(self, **kwargs) -> None:
            self.device = kwargs["device"]
            built.append(Path(kwargs["model"]).name)
            manager._selected_model = "funasr-nano"  # picked while it loads

        def generate(self, **kwargs):
            inputs.append(kwargs["input"])
            if self.device == "cuda:0":
                raise RuntimeError("gpu transcription failed")
            return [{"text": "hello"}]

    install_fake_funasr(monkeypatch, FakeFunASRModel)
    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    )
    transcriber = Transcriber(manager)

    text = transcriber.transcribe(
        SPEECH, make_settings(tmp_path, device="gpu", use_gpu_if_available=True)
    )

    assert text == "hello"
    assert built == ["FunAudioLLM--SenseVoiceSmall"] * 2  # the CPU retry too
    assert len(inputs) == 2 and all(entry is SPEECH for entry in inputs)  # not Nano's list
    assert manager.marked == ["sensevoice-small"]


def test_huggingface_requests_get_a_deadline_before_huggingface_hub_loads() -> None:
    # huggingface_hub reads its timeouts once, when it is imported.
    env = {key: value for key, value in os.environ.items() if not key.startswith("HF_HUB_")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    probe = (
        "import core.transcriber\n"
        "from huggingface_hub import constants\n"
        "print(constants.HF_HUB_ETAG_TIMEOUT, constants.HF_HUB_DOWNLOAD_TIMEOUT)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["30", "30"]


def test_resolve_runtime_options_uses_cpu_defaults_when_gpu_is_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcriber = Transcriber(FakeModelManager())
    monkeypatch.setattr(transcriber, "_cuda_available", lambda: False)
    monkeypatch.setattr(
        transcriber, "_supported_compute_types", lambda device: {"int8", "float32"}
    )

    runtime = transcriber._resolve_runtime_options(make_settings(tmp_path))

    assert runtime == RuntimeOptions(
        device="cpu", compute_type="int8", cpu_threads=None
    )
    assert transcriber.runtime_message == ""


def test_resolve_runtime_options_converts_unsupported_precision_to_safe_cpu_value(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcriber = Transcriber(FakeModelManager())
    monkeypatch.setattr(transcriber, "_cuda_available", lambda: False)
    monkeypatch.setattr(
        transcriber, "_supported_compute_types", lambda device: {"int8", "float32"}
    )

    runtime = transcriber._resolve_runtime_options(
        make_settings(tmp_path, device="cpu", compute_type="float16")
    )

    assert runtime == RuntimeOptions(
        device="cpu", compute_type="int8", cpu_threads=None
    )
    assert (
        "Compute precision float16 is not supported on CPU, so int8 was used."
        in transcriber.runtime_message
    )


def test_load_model_falls_back_to_cpu_when_gpu_load_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = []

    class FakeWhisperModel:
        def __init__(self, backend_name: str, **kwargs) -> None:
            created.append((backend_name, dict(kwargs)))
            self.backend_name = backend_name
            self.kwargs = dict(kwargs)
            if kwargs["device"] == "cuda":
                raise RuntimeError("cuda init failed")

    downloads = install_fake_whisper(monkeypatch, FakeWhisperModel)

    transcriber = Transcriber(FakeModelManager())
    monkeypatch.setattr(transcriber, "_cuda_available", lambda: True)
    monkeypatch.setattr(
        transcriber,
        "_supported_compute_types",
        lambda device: (
            {"float16", "int8_float16", "float32"}
            if device == "cuda"
            else {"int8", "float32"}
        ),
    )

    model = transcriber._load_model(
        make_settings(tmp_path, device="gpu", use_gpu_if_available=True), "base"
    )

    assert model.kwargs["device"] == "cpu"
    assert model.kwargs["compute_type"] == "int8"
    assert [entry[1]["device"] for entry in created] == ["cuda", "cpu"]
    assert len(downloads) == 1  # the CPU load reuses the downloaded folder
    assert (
        transcriber.runtime_message
        == "GPU loading failed, so Whisper Anywhere continued on CPU with safe int8 precision."
    )


def test_transcribe_retries_on_cpu_when_gpu_transcription_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeSegment:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeWhisperModel:
        def __init__(self, backend_name: str, **kwargs) -> None:
            self.kwargs = dict(kwargs)

        def transcribe(self, audio_path: str, **kwargs):
            if self.kwargs["device"] == "cuda":
                raise RuntimeError("gpu transcription failed")
            return [FakeSegment("  hello"), FakeSegment("world  ")], {"language": "en"}

    install_fake_whisper(monkeypatch, FakeWhisperModel)

    manager = FakeModelManager()
    transcriber = Transcriber(manager)
    monkeypatch.setattr(transcriber, "_cuda_available", lambda: True)
    monkeypatch.setattr(
        transcriber,
        "_supported_compute_types",
        lambda device: (
            {"float16", "int8_float16", "float32"}
            if device == "cuda"
            else {"int8", "float32"}
        ),
    )
    monkeypatch.setattr(transcriber, "_looks_like_silence", lambda audio_path: False)

    text = transcriber.transcribe(
        "clip.wav",
        make_settings(
            tmp_path, device="gpu", use_gpu_if_available=True, fallback_to_cpu=True
        ),
    )

    assert text == "hello world"
    assert manager.ready_marks == 1
    assert (
        transcriber.runtime_message
        == "GPU transcription failed, so Whisper Anywhere continued on CPU with safe int8 precision."
    )
    assert transcriber._model is not None
    assert transcriber._loaded_key is not None
    assert transcriber._loaded_key[0] == "whisper"
    assert transcriber._loaded_key[3] == "cpu"


def test_transcribe_unloads_whisper_when_set_to_unload_after_each_dictation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeSegment:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeWhisperModel:
        def transcribe(self, audio_path: str, **kwargs):
            return [FakeSegment("hello")], {"language": "en"}

        def __init__(self, backend_name: str, **kwargs) -> None:
            self.kwargs = dict(kwargs)

    install_fake_whisper(monkeypatch, FakeWhisperModel)

    transcriber = Transcriber(FakeModelManager())
    monkeypatch.setattr(transcriber, "_cuda_available", lambda: False)
    monkeypatch.setattr(
        transcriber, "_supported_compute_types", lambda device: {"int8", "float32"}
    )
    monkeypatch.setattr(transcriber, "_looks_like_silence", lambda audio_path: False)

    text = transcriber.transcribe(
        "clip.wav", make_settings(tmp_path, device="cpu", unload_model_after="each")
    )

    assert text == "hello"
    assert transcriber._model is None
    assert transcriber._loaded_key is None


def test_transcribe_with_funasr_uses_automodel_and_cleans_tags(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = []
    generated = []

    class FakeFunASRModel:
        def __init__(self, **kwargs) -> None:
            created.append(dict(kwargs))

        def generate(self, **kwargs):
            generated.append(dict(kwargs))
            return [{"text": "<|en|><|NEUTRAL|><|Speech|><|withitn|> hello funasr "}]

    install_fake_funasr(monkeypatch, FakeFunASRModel)
    monkeypatch.setattr(Transcriber, "_looks_like_silence", lambda self, path: False)

    manager = FakeModelManager("sensevoice-small")
    transcriber = Transcriber(manager)

    text = transcriber.transcribe(
        "clip.wav",
        make_settings(tmp_path, device="cpu", selected_model="sensevoice-small"),
    )
    text_again = transcriber.transcribe(
        "clip.wav",
        make_settings(tmp_path, device="cpu", selected_model="sensevoice-small"),
    )

    hub = tmp_path / "models" / "funasr" / "huggingface" / "hub"
    assert text == "hello funasr"
    assert text_again == "hello funasr"
    assert created == [
        {
            "model": str(hub / "FunAudioLLM--SenseVoiceSmall"),
            "device": "cpu",
            "disable_update": True,
            "disable_pbar": True,
            "disable_log": True,
            "ncpu": default_cpu_threads(),
            "hub": "hf",
            "vad_model": str(hub / "funasr--fsmn-vad"),
            "vad_kwargs": {"max_single_segment_time": 30000},
        }
    ]
    assert (
        generated
        == [
            {
                "input": "clip.wav",
                "cache": {},
                "use_itn": True,
                "batch_size_s": 60,
                "disable_pbar": True,
                "disable_log": True,
                "merge_vad": True,
                "merge_length_s": 15,
                "language": "auto",
            }
        ]
        * 2
    )
    assert manager.marked == ["sensevoice-small"]  # once, not after every dictation
    assert transcriber._model is not None
    assert transcriber._loaded_key is not None
    assert transcriber._loaded_key[0] == "funasr"


def test_funasr_retries_on_cpu_when_gpu_transcription_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_devices = []

    class FakeFunASRModel:
        def __init__(self, **kwargs) -> None:
            self.kwargs = dict(kwargs)
            created_devices.append(self.kwargs["device"])

        def generate(self, **kwargs):
            if self.kwargs["device"] == "cuda:0":
                raise RuntimeError("gpu transcription failed")
            return [{"text": "cpu funasr"}]

    install_fake_funasr(monkeypatch, FakeFunASRModel)
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)),
    )
    monkeypatch.setattr(Transcriber, "_looks_like_silence", lambda self, path: False)

    manager = FakeModelManager("sensevoice-small")
    transcriber = Transcriber(manager)

    text = transcriber.transcribe(
        "clip.wav",
        make_settings(
            tmp_path,
            device="gpu",
            use_gpu_if_available=True,
            fallback_to_cpu=True,
            selected_model="sensevoice-small",
        ),
    )

    assert text == "cpu funasr"
    assert created_devices == ["cuda:0", "cpu"]
    assert manager.ready_marks == 1
    assert transcriber.runtime_message == (
        "GPU transcription failed, so FunASR continued on CPU."
    )
    transcriber.transcribe(
        "clip.wav",
        make_settings(
            tmp_path,
            device="gpu",
            use_gpu_if_available=True,
            fallback_to_cpu=True,
            selected_model="sensevoice-small",
        ),
    )
    assert created_devices == ["cuda:0", "cpu"]  # the GPU is not tried again


def test_a_failed_gpu_is_not_tried_again_on_every_dictation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing cuBLAS (the installer build has none) failed the GPU every
    # time, so each dictation built a CUDA model and then a CPU one.
    created = []

    class FakeWhisperModel:
        def __init__(self, backend_name: str, **kwargs) -> None:
            created.append(kwargs["device"])
            if kwargs["device"] == "cuda":
                raise RuntimeError("cublas64_12.dll is not found")

    install_fake_whisper(monkeypatch, FakeWhisperModel)
    transcriber = Transcriber(FakeModelManager())
    monkeypatch.setattr(transcriber, "_cuda_available", lambda: True)
    settings = make_settings(tmp_path, device="gpu", use_gpu_if_available=True)

    transcriber._load_model(settings, "base")
    transcriber._load_model(settings, "base")  # kept loaded: the CPU model is reused
    transcriber.unload()  # the idle timer, or "after every dictation"
    transcriber._load_model(settings, "base")

    assert created == ["cuda", "cpu", "cpu"]


def test_a_silent_tap_does_not_stay_the_status_of_later_dictations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.voz_engine as voz_engine

    class FakeVoz:
        def __init__(self, model_root, cpu_threads=None) -> None:
            pass

        def warm_up(self) -> None:
            pass

        def transcribe(self, samples) -> str:
            return "hello"

    monkeypatch.setattr(voz_engine, "VozModel", FakeVoz)
    monkeypatch.setattr(voz_engine, "missing_files", lambda root: [])
    transcriber = Transcriber(FakeModelManager("voz"))
    settings = make_settings(tmp_path, selected_model="voz")
    speech = np.full(16000, 0.1, np.float32)

    transcriber.transcribe(speech, settings)
    transcriber.transcribe(np.zeros(16000, np.float32), settings)
    assert transcriber.runtime_message.startswith("No speech")
    assert transcriber.transcribe(speech, settings) == "hello"

    assert transcriber.runtime_message == ""


def test_a_quick_tap_is_too_short_rather_than_silent(tmp_path) -> None:
    # The recorder returns no samples for a tap that ended before the first
    # buffer; it is not quiet speech under the noise floor (AU-5).
    transcriber = Transcriber(FakeModelManager("voz"))
    settings = make_settings(tmp_path)

    assert transcriber.transcribe(np.zeros(0, np.float32), settings) == ""
    assert transcriber.runtime_message == "The recording was too short."
    assert transcriber.transcribe(np.zeros(16000, np.float32), settings) == ""
    assert transcriber.runtime_message.startswith("No speech")
    assert not transcriber.is_loaded()


def test_forcing_chinese_on_sensevoice_asks_for_zh() -> None:
    # SenseVoice only knows "zh"; "zn" quietly fell back to auto-detect.
    transcriber = Transcriber(FakeModelManager("sensevoice-small"))

    for spoken in ("zh", "zh-cn", "cmn", "mandarin"):
        settings = {"language_mode": "force", "forced_language": spoken}
        assert transcriber._funasr_language(settings) == "zh"


def test_the_old_preset_menu_no_longer_overrides_the_window(tmp_path) -> None:
    # v0.2 shows no preset; a saved v0.1 "low_ram" preset still unloaded the
    # model and forced the CPU while the window said otherwise.
    transcriber = Transcriber(FakeModelManager())
    transcriber._cuda_available = lambda: True
    settings = make_settings(
        tmp_path,
        performance_preset="low_ram",
        device="gpu",
        use_gpu_if_available=True,
    )

    assert transcriber._resolve_device(settings, []) == "cuda"


def test_voz_loads_once_and_silence_does_not_unload_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.voz_engine as voz_engine

    created = []

    class FakeVoz:
        def __init__(self, model_root, cpu_threads=None) -> None:
            created.append((model_root, cpu_threads))

        def warm_up(self) -> None:
            pass

        def transcribe(self, samples) -> str:
            assert samples.dtype == np.float32
            return "  hello   voz "

    monkeypatch.setattr(voz_engine, "VozModel", FakeVoz)
    monkeypatch.setattr(voz_engine, "missing_files", lambda root: [])
    transcriber = Transcriber(FakeModelManager("voz"))
    settings = make_settings(tmp_path, selected_model="voz")
    speech = np.full(16000, 0.1, np.float32)

    assert transcriber.transcribe(speech, settings) == "hello voz"
    assert transcriber.transcribe(np.zeros(16000, np.float32), settings) == ""
    assert transcriber.transcribe(speech, settings) == "hello voz"
    assert len(created) == 1
    assert transcriber.is_loaded()


def test_funasr_gets_the_recording_in_memory(tmp_path, monkeypatch) -> None:
    # It used to get a WAV in %TEMP%, left behind if the app exited mid-way.
    import tempfile

    monkeypatch.setattr(tempfile, "mkstemp", lambda *a, **k: pytest.fail("speech written to disk"))
    inputs: list[object] = []

    class FakeFunASRModel:
        def __init__(self, **kwargs) -> None:
            pass

        def generate(self, **kwargs):
            inputs.append(kwargs["input"])
            return [{"text": "hello"}]

    install_fake_funasr(monkeypatch, FakeFunASRModel)
    speech = np.full(16000, 0.1, np.float32)

    for model in ("sensevoice-small", "funasr-nano"):
        transcriber = Transcriber(FakeModelManager(model))
        assert transcriber.transcribe(speech, make_settings(tmp_path, device="cpu")) == "hello"

    assert inputs[0] is speech and inputs[1][0] is speech  # Nano takes a list


def test_sensevoice_types_its_words_without_emoji_and_noise_types_nothing() -> None:
    # funasr's display formatter turned SenseVoice's tags into emoji, typed
    # "❓." for a noise-only tap and deleted every "The." (SW-3).
    transcriber = Transcriber(FakeModelManager("sensevoice-small"))

    def typed(raw: str) -> str:
        return transcriber._extract_funasr_text([{"text": raw}])

    assert typed("<|en|><|HAPPY|><|Speech|><|withitn|>Good game everyone.") == "Good game everyone."
    assert typed("<|en|><|NEUTRAL|><|BGM|><|withitn|>Push mid now.") == "Push mid now."
    assert typed("<|en|><|NEUTRAL|><|Speech|><|withitn|>The. Plan is simple.") == "The. Plan is simple."
    assert typed("<|ja|><|NEUTRAL|><|Speech|><|withitn|>こんにちは。") == "こんにちは。"
    assert typed("<|nospeech|><|Event_UNK|>") == ""
    assert typed("<|en|><|NEUTRAL|><|Laughter|><|withitn|>.") == ""


def test_extract_funasr_text_prefers_sentence_info() -> None:
    transcriber = Transcriber(FakeModelManager("sensevoice-small"))

    text = transcriber._extract_funasr_text(
        [
            {
                "text": "full duplicate text",
                "sentence_info": [
                    {"text": "<|en|><|Speech|>first"},
                    {"text": "second"},
                ],
            }
        ]
    )

    assert text == "first second"
