from __future__ import annotations

from pathlib import Path
from typing import Any

from core.model_manager import ModelManager


class TranscriptionError(RuntimeError):
    pass


class Transcriber:
    def __init__(self, model_manager: ModelManager) -> None:
        self.model_manager = model_manager
        self._model = None
        self._loaded_key: tuple[str, str, str, str] | None = None

    def is_selected_model_ready(self) -> bool:
        return self.model_manager.is_model_ready()

    def prepare_selected_model(self, settings: dict[str, Any]) -> None:
        self._load_model(settings)
        self.model_manager.mark_model_ready()

    def transcribe(self, audio_path: str, settings: dict[str, Any]) -> str:
        model = self._load_model(settings)
        kwargs: dict[str, Any] = {
            "beam_size": 5,
            "vad_filter": False,
            "condition_on_previous_text": False,
        }

        language_mode = str(settings.get("language_mode", "auto"))
        forced_language = str(settings.get("forced_language", "")).strip()
        if language_mode == "force" and forced_language:
            kwargs["language"] = forced_language

        if bool(settings.get("translate_to_english", False)):
            kwargs["task"] = "translate"

        try:
            segments, _info = model.transcribe(audio_path, **kwargs)
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            raise TranscriptionError(
                "Transcription failed. Try a smaller model, check your microphone audio, or switch to CPU mode."
            ) from exc

        self.model_manager.mark_model_ready()
        return " ".join(text.split())

    def _load_model(self, settings: dict[str, Any]):
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise TranscriptionError(
                "The local Whisper engine is not installed. Reinstall Whisper Anywhere or rebuild the app with dependencies included."
            ) from exc

        model_name = self.model_manager.selected_model()
        backend_name = self.model_manager.backend_model_name(model_name)
        model_path = str(Path(str(settings.get("model_path"))).expanduser())
        device = "cuda" if bool(settings.get("use_gpu", False)) else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        key = (backend_name, model_path, device, compute_type)

        if self._model is not None and self._loaded_key == key:
            return self._model

        try:
            self._model = WhisperModel(
                backend_name,
                device=device,
                compute_type=compute_type,
                download_root=model_path,
                local_files_only=False,
            )
            self._loaded_key = key
            return self._model
        except Exception as exc:
            if device == "cuda":
                raise TranscriptionError(
                    "The selected Whisper model could not load on GPU. Turn off GPU mode or install compatible CUDA libraries."
                ) from exc
            raise TranscriptionError(
                "The selected Whisper model could not be loaded or downloaded. Check your internet connection, disk space, and model folder permissions."
            ) from exc
