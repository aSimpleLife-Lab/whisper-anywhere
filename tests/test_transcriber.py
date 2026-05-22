from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from core.settings_manager import DEFAULT_SETTINGS
from core.transcriber import RuntimeOptions, Transcriber


class FakeModelManager:
    def __init__(self, selected_model: str = "base") -> None:
        self._selected_model = selected_model
        self.ready_marks = 0

    def is_model_ready(self) -> bool:
        return False

    def mark_model_ready(self) -> None:
        self.ready_marks += 1

    def selected_model(self) -> str:
        return self._selected_model

    def backend_model_name(self, model_name: str | None = None) -> str:
        selected = model_name or self._selected_model
        if selected == "turbo":
            return "large-v3-turbo"
        return selected


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
            "low_ram_mode": False,
            "low_vram_mode": False,
            "cpu_threads": "auto",
            "language_mode": "auto",
            "forced_language": "",
            "translate_to_english": False,
        }
    )
    settings.update(overrides)
    return settings


def install_fake_whisper(monkeypatch: pytest.MonkeyPatch, whisper_model_class) -> None:
    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper_model_class)
    )


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

    install_fake_whisper(monkeypatch, FakeWhisperModel)

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
        make_settings(tmp_path, device="gpu", use_gpu_if_available=True)
    )

    assert model.kwargs["device"] == "cpu"
    assert model.kwargs["compute_type"] == "int8"
    assert [entry[1]["device"] for entry in created] == ["cuda", "cpu"]
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
    assert transcriber._model is None
    assert transcriber._loaded_key is None
