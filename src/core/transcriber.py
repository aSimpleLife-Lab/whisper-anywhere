from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import wave

from core.model_manager import ModelManager


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeOptions:
    device: str
    compute_type: str
    cpu_threads: int | None


class Transcriber:
    def __init__(self, model_manager: ModelManager) -> None:
        self.model_manager = model_manager
        self._model = None
        self._loaded_key: tuple[str, str, str, str, int | None] | None = None
        self.runtime_message = ""

    def is_selected_model_ready(self) -> bool:
        return self.model_manager.is_model_ready()

    def prepare_selected_model(self, settings: dict[str, Any]) -> None:
        try:
            self._load_model(settings)
            self.model_manager.mark_model_ready()
        finally:
            self.reset()

    def transcribe(self, audio_path: str, settings: dict[str, Any]) -> str:
        if self._looks_like_silence(audio_path):
            self.runtime_message = "No speech was detected above the microphone noise floor."
            self._model = None
            self._loaded_key = None
            return ""
        try:
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
                if self._should_retry_on_cpu(settings):
                    try:
                        fallback_model = self._load_cpu_fallback_model(settings)
                        segments, _info = fallback_model.transcribe(audio_path, **kwargs)
                        text = " ".join(segment.text.strip() for segment in segments).strip()
                    except Exception as fallback_exc:
                        raise TranscriptionError(
                            "Transcription failed. Try a smaller model, check your microphone audio, or switch to CPU mode."
                        ) from fallback_exc
                else:
                    raise TranscriptionError(
                        "Transcription failed. Try a smaller model, check your microphone audio, or switch to CPU mode."
                    ) from exc

            self.model_manager.mark_model_ready()
            return " ".join(text.split())
        finally:
            self.reset()

    def reset(self) -> None:
        self._model = None
        self._loaded_key = None

    def _looks_like_silence(self, audio_path: str) -> bool:
        path = Path(audio_path)
        if path.suffix.lower() != ".wav" or not path.exists():
            return False
        try:
            with wave.open(str(path), "rb") as wav_file:
                frames = wav_file.readframes(wav_file.getnframes())
        except (OSError, wave.Error):
            return False
        samples = array("h")
        samples.frombytes(frames)
        if not samples:
            return True
        peak = max(abs(value) for value in samples)
        avg_abs = sum(abs(value) for value in samples) / len(samples)
        return peak < 300 and avg_abs < 25

    def _load_model(self, settings: dict[str, Any]):
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise TranscriptionError(
                "The local Whisper engine is not installed. Reinstall Whisper Anywhere or rebuild the app with dependencies included."
            ) from exc

        self.runtime_message = ""
        model_name = self.model_manager.selected_model()
        backend_name = self.model_manager.backend_model_name(model_name)
        model_path = str(Path(str(settings.get("model_path"))).expanduser())
        runtime = self._resolve_runtime_options(settings)
        key = (backend_name, model_path, runtime.device, runtime.compute_type, runtime.cpu_threads)

        if self._model is not None and self._loaded_key == key:
            return self._model

        try:
            self._model = self._create_model(WhisperModel, backend_name, model_path, runtime)
            self._loaded_key = key
            return self._model
        except Exception as exc:
            if runtime.device == "cuda" and bool(settings.get("fallback_to_cpu", True)):
                fallback_runtime = RuntimeOptions(device="cpu", compute_type="int8", cpu_threads=self._parse_cpu_threads(settings))
                fallback_key = (backend_name, model_path, fallback_runtime.device, fallback_runtime.compute_type, fallback_runtime.cpu_threads)
                try:
                    self._model = self._create_model(WhisperModel, backend_name, model_path, fallback_runtime)
                    self._loaded_key = fallback_key
                    self.runtime_message = "GPU loading failed, so Whisper Anywhere continued on CPU with safe int8 precision."
                    return self._model
                except Exception as fallback_exc:
                    raise TranscriptionError(
                        "The selected Whisper model could not load on GPU or CPU. Try a smaller model or check disk space and model folder permissions."
                    ) from fallback_exc

            if runtime.device == "cuda":
                raise TranscriptionError(
                    "The selected Whisper model could not load on GPU. Turn on CPU fallback or select CPU only."
                ) from exc
            raise TranscriptionError(
                "The selected Whisper model could not be loaded or downloaded. Check your internet connection, disk space, and model folder permissions."
            ) from exc

    def _create_model(self, whisper_model_class: Any, backend_name: str, model_path: str, runtime: RuntimeOptions):
        kwargs: dict[str, Any] = {
            "device": runtime.device,
            "compute_type": runtime.compute_type,
            "download_root": model_path,
            "local_files_only": False,
        }
        if runtime.device == "cpu" and runtime.cpu_threads is not None:
            kwargs["cpu_threads"] = runtime.cpu_threads
        return whisper_model_class(backend_name, **kwargs)

    def _should_retry_on_cpu(self, settings: dict[str, Any]) -> bool:
        return bool(settings.get("fallback_to_cpu", True)) and self._loaded_key is not None and self._loaded_key[2] == "cuda"

    def _load_cpu_fallback_model(self, settings: dict[str, Any]):
        from faster_whisper import WhisperModel

        model_name = self.model_manager.selected_model()
        backend_name = self.model_manager.backend_model_name(model_name)
        model_path = str(Path(str(settings.get("model_path"))).expanduser())
        fallback_runtime = RuntimeOptions(device="cpu", compute_type="int8", cpu_threads=self._parse_cpu_threads(settings))
        fallback_key = (backend_name, model_path, fallback_runtime.device, fallback_runtime.compute_type, fallback_runtime.cpu_threads)
        self._model = self._create_model(WhisperModel, backend_name, model_path, fallback_runtime)
        self._loaded_key = fallback_key
        self.runtime_message = "GPU transcription failed, so Whisper Anywhere continued on CPU with safe int8 precision."
        return self._model

    def _resolve_runtime_options(self, settings: dict[str, Any]) -> RuntimeOptions:
        messages: list[str] = []
        device = self._resolve_device(settings, messages)
        compute_type = self._resolve_compute_type(device, settings, messages)
        cpu_threads = self._parse_cpu_threads(settings) if device == "cpu" else None
        self.runtime_message = " ".join(messages).strip()
        return RuntimeOptions(device=device, compute_type=compute_type, cpu_threads=cpu_threads)

    def _resolve_device(self, settings: dict[str, Any], messages: list[str]) -> str:
        if bool(settings.get("low_ram_mode", False)) or str(settings.get("performance_preset", "balanced")) == "low_ram":
            return "cpu"

        requested = str(settings.get("device", "auto"))
        use_gpu_if_available = bool(settings.get("use_gpu_if_available", True))
        fallback_to_cpu = bool(settings.get("fallback_to_cpu", True))

        if requested == "cpu":
            return "cpu"

        if requested == "gpu" and not use_gpu_if_available:
            messages.append("GPU is disabled in settings, so CPU was used.")
            return "cpu"

        cuda_available = self._cuda_available()
        if requested == "gpu":
            if cuda_available:
                return "cuda"
            if fallback_to_cpu:
                messages.append("A compatible CUDA GPU was not found, so CPU was used.")
                return "cpu"
            raise TranscriptionError("GPU preferred is selected, but a compatible CUDA GPU was not found. Turn on CPU fallback or choose CPU only.")

        if requested == "auto" and use_gpu_if_available and cuda_available:
            return "cuda"
        return "cpu"

    def _resolve_compute_type(self, device: str, settings: dict[str, Any], messages: list[str]) -> str:
        requested = str(settings.get("compute_type", "auto"))
        preset = str(settings.get("performance_preset", "balanced"))
        low_vram = bool(settings.get("low_vram_mode", False)) or preset == "low_vram"
        supported = self._supported_compute_types(device)

        if requested == "auto":
            if device == "cpu":
                requested = "int8"
            elif low_vram or preset == "fast":
                requested = "int8_float16"
            else:
                requested = "float16"

        if requested in supported:
            return requested

        fallback_order = ["int8", "float32"] if device == "cpu" else ["float16", "int8_float16", "float32", "int8"]
        for candidate in fallback_order:
            if candidate in supported:
                messages.append(f"Compute precision {requested} is not supported on {device.upper()}, so {candidate} was used.")
                return candidate

        safe = "int8" if device == "cpu" else "float16"
        messages.append(f"Compute precision {requested} may not be supported, so {safe} was selected as a safe default.")
        return safe

    def _parse_cpu_threads(self, settings: dict[str, Any]) -> int | None:
        raw_threads = settings.get("cpu_threads", "auto")
        if raw_threads == "auto":
            return None
        try:
            return max(1, min(64, int(raw_threads)))
        except (TypeError, ValueError):
            return None

    def _cuda_available(self) -> bool:
        try:
            import ctranslate2
        except Exception:
            return False

        try:
            get_count = getattr(ctranslate2, "get_cuda_device_count", None)
            if get_count is not None:
                return int(get_count()) > 0
        except Exception:
            return False

        try:
            try:
                supported = ctranslate2.get_supported_compute_types("cuda")
            except TypeError:
                supported = ctranslate2.get_supported_compute_types("cuda", 0)
            return bool(supported)
        except Exception:
            return False

    def _supported_compute_types(self, device: str) -> set[str]:
        try:
            import ctranslate2

            try:
                supported = ctranslate2.get_supported_compute_types(device)
            except TypeError:
                supported = ctranslate2.get_supported_compute_types(device, 0)
            return {str(item) for item in supported}
        except Exception:
            if device == "cpu":
                return {"int8", "float32"}
            return {"int8", "int8_float16", "float16", "float32"}
