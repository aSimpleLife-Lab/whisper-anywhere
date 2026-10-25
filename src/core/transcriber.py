from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from typing import Any, Iterator
import wave

import numpy as np

from core.model_manager import ModelManager

SAMPLE_RATE = 16000
# Below both of these the clip is microphone hiss, not speech.
SILENCE_PEAK = 300 / 32768
SILENCE_MEAN = 25 / 32768

Audio = np.ndarray | str


class TranscriptionError(RuntimeError):
    pass


def default_cpu_threads() -> int:
    """Physical cores, roughly: SMT siblings only slow these models down."""
    return max(4, min(16, (os.cpu_count() or 8) // 2))


def read_wav(path: str) -> np.ndarray | None:
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            rate = wav_file.getframerate()
            frames = wav_file.readframes(wav_file.getnframes())
    except (OSError, EOFError, wave.Error):
        return None
    samples = np.frombuffer(frames, np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and samples.size:
        positions = np.arange(0, samples.size, rate / SAMPLE_RATE)
        samples = np.interp(positions, np.arange(samples.size), samples)
    return samples.astype(np.float32)


def destroy_file(path: Path) -> None:
    """Overwrite (10 alternating 0x00/0xFF passes + 1 random), rename, delete."""
    try:
        size = path.stat().st_size
        with path.open("r+b") as file:
            for fill in [b"\x00", b"\xff"] * 5 + [None]:
                file.seek(0)
                file.write(os.urandom(size) if fill is None else fill * size)
                file.flush()
                os.fsync(file.fileno())
        renamed = path.with_name(os.urandom(8).hex())
        path.replace(renamed)
        renamed.unlink()
    except OSError:
        path.unlink(missing_ok=True)


@contextmanager
def temporary_wav(samples: np.ndarray) -> Iterator[str]:
    """For engines that only read files: a WAV that is destroyed afterwards."""
    handle, name = tempfile.mkstemp(suffix=".wav", prefix="wa-")
    os.close(handle)
    path = Path(name)
    try:
        pcm = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
        with wave.open(name, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm.tobytes())
        yield name
    finally:
        destroy_file(path)


@dataclass(frozen=True)
class RuntimeOptions:
    device: str
    compute_type: str
    cpu_threads: int | None


class Transcriber:
    def __init__(self, model_manager: ModelManager) -> None:
        self.model_manager = model_manager
        self._model = None
        self._loaded_key: tuple[Any, ...] | None = None
        self.runtime_message = ""
        # A background warm-up and a dictation never load the model twice.
        self._lock = threading.RLock()

    def is_selected_model_ready(self) -> bool:
        return self.model_manager.is_model_ready()

    def is_loaded(self) -> bool:
        return self._model is not None

    def prepare_selected_model(self, settings: dict[str, Any]) -> None:
        keep_loaded = self._should_keep_model_loaded(settings)
        with self._lock:
            try:
                self._load_model(settings)
                self.model_manager.mark_model_ready()
            except Exception:
                self.reset()
                raise
            else:
                if not keep_loaded:
                    self.reset()

    def transcribe(self, audio: Audio, settings: dict[str, Any]) -> str:
        """Transcribe 16 kHz mono float32 samples (or a WAV path)."""
        with self._lock:
            if self._looks_like_silence(audio):
                # The model stays loaded: an accidental tap of the shortcut
                # should not make the next real dictation reload it.
                self.runtime_message = (
                    "No speech was detected above the microphone noise floor."
                )
                return ""
            keep_loaded = self._should_keep_model_loaded(settings)
            try:
                engine = self.model_manager.model_engine(
                    self.model_manager.selected_model()
                )
                if engine == "voz":
                    text = self._transcribe_with_voz(audio, settings)
                elif engine == "funasr":
                    text = self._transcribe_with_funasr(audio, settings)
                else:
                    text = self._transcribe_with_whisper(audio, settings)
                self.model_manager.mark_model_ready()
                return " ".join(text.split())
            except Exception:
                self.reset()
                raise
            finally:
                if not keep_loaded:
                    self.reset()

    def reset(self) -> None:
        with self._lock:
            self._model = None
            self._loaded_key = None

    def _should_keep_model_loaded(self, settings: dict[str, Any]) -> bool:
        if not bool(settings.get("keep_model_loaded", True)):
            return False
        if bool(settings.get("low_ram_mode", False)):
            return False
        return str(settings.get("performance_preset", "balanced")) != "low_ram"

    def _looks_like_silence(self, audio: Audio) -> bool:
        if isinstance(audio, np.ndarray):
            samples = audio
        else:
            path = Path(audio)
            if path.suffix.lower() != ".wav" or not path.exists():
                return False
            samples = read_wav(str(path))
            if samples is None:
                return False
        if samples.size == 0:
            return True
        magnitude = np.abs(samples)
        return float(magnitude.max()) < SILENCE_PEAK and float(
            magnitude.mean()
        ) < SILENCE_MEAN

    def _transcribe_with_voz(self, audio: Audio, settings: dict[str, Any]) -> str:
        model = self._load_model(settings)
        if bool(settings.get("translate_to_english", False)):
            self._append_runtime_message(
                "Voz transcribes speech but does not translate it to English."
            )
        samples = audio if isinstance(audio, np.ndarray) else read_wav(audio)
        if samples is None:
            raise TranscriptionError("The recording could not be read.")
        try:
            return model.transcribe(samples)
        except Exception as exc:
            raise TranscriptionError(
                "Voz transcription failed. Check your microphone audio or try another speech model."
            ) from exc

    def _load_voz_model(self, settings: dict[str, Any]):
        from core import voz_engine

        model_path = str(Path(str(settings.get("model_path"))).expanduser())
        threads = self._parse_cpu_threads(settings)
        key = ("voz", voz_engine.REVISION, model_path, "cpu", "float32", threads)
        if self._model is not None and self._loaded_key == key:
            return self._model
        self.runtime_message = (
            "Voz runs on the CPU, so the GPU setting does not apply to it."
            if str(settings.get("device", "auto")) == "gpu"
            else ""
        )
        try:
            if voz_engine.missing_files(model_path):
                self._enable_system_certificate_store()
                voz_engine.download_bundle(model_path)
            model = voz_engine.VozModel(model_path, cpu_threads=threads)
            model.warm_up()
        except Exception as exc:
            raise TranscriptionError(
                "Voz could not be downloaded or loaded. Check your internet connection, disk space, and the model folder."
            ) from exc
        self._model = model
        self._loaded_key = key
        return model

    def _transcribe_with_whisper(self, audio: Audio, settings: dict[str, Any]) -> str:
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
            segments, _info = model.transcribe(audio, **kwargs)
            return " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            if self._should_retry_on_cpu(settings):
                try:
                    fallback_model = self._load_cpu_fallback_model(settings)
                    segments, _info = fallback_model.transcribe(audio, **kwargs)
                    return " ".join(
                        segment.text.strip() for segment in segments
                    ).strip()
                except Exception as fallback_exc:
                    raise TranscriptionError(
                        "Transcription failed. Try a smaller model, check your microphone audio, or switch to CPU mode."
                    ) from fallback_exc
            raise TranscriptionError(
                "Transcription failed. Try a smaller model, check your microphone audio, or switch to CPU mode."
            ) from exc

    def _transcribe_with_funasr(self, audio: Audio, settings: dict[str, Any]) -> str:
        if isinstance(audio, np.ndarray):
            # FunASR reads files, so it gets a short-lived WAV.
            with temporary_wav(audio) as path:
                return self._transcribe_with_funasr(path, settings)
        model = self._load_model(settings)
        kwargs = self._funasr_generate_kwargs(audio, settings)
        if bool(settings.get("translate_to_english", False)):
            self._append_runtime_message(
                "FunASR transcribes speech but does not translate it to English."
            )
        try:
            result = model.generate(**kwargs)
            return self._extract_funasr_text(result)
        except Exception as exc:
            if self._should_retry_funasr_on_cpu(settings):
                try:
                    fallback_model = self._load_funasr_cpu_fallback_model(settings)
                    result = fallback_model.generate(**kwargs)
                    return self._extract_funasr_text(result)
                except Exception as fallback_exc:
                    raise TranscriptionError(
                        "FunASR transcription failed. Try CPU mode, check your microphone audio, or use a smaller speech model."
                    ) from fallback_exc
            raise TranscriptionError(
                "FunASR transcription failed. Try CPU mode, check your microphone audio, or use a smaller speech model."
            ) from exc

    def _load_model(self, settings: dict[str, Any]):
        model_name = self.model_manager.selected_model()
        engine = self.model_manager.model_engine(model_name)
        if engine == "voz":
            return self._load_voz_model(settings)
        if engine == "funasr":
            return self._load_funasr_model(settings, model_name)
        return self._load_whisper_model(settings, model_name)

    def _load_whisper_model(self, settings: dict[str, Any], model_name: str):
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise TranscriptionError(
                "The local Whisper engine is not installed. Reinstall Whisper Anywhere or rebuild the app with dependencies included."
            ) from exc

        self.runtime_message = ""
        backend_name = self.model_manager.backend_model_name(model_name)
        model_path = str(Path(str(settings.get("model_path"))).expanduser())
        runtime = self._resolve_runtime_options(settings)
        key = (
            "whisper",
            backend_name,
            model_path,
            runtime.device,
            runtime.compute_type,
            runtime.cpu_threads,
        )

        if self._model is not None and self._loaded_key == key:
            return self._model

        try:
            self._model = self._create_model(
                WhisperModel, backend_name, model_path, runtime
            )
            self._loaded_key = key
            return self._model
        except Exception as exc:
            if runtime.device == "cuda" and bool(settings.get("fallback_to_cpu", True)):
                fallback_runtime = RuntimeOptions(
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=self._parse_cpu_threads(settings),
                )
                fallback_key = (
                    "whisper",
                    backend_name,
                    model_path,
                    fallback_runtime.device,
                    fallback_runtime.compute_type,
                    fallback_runtime.cpu_threads,
                )
                try:
                    self._model = self._create_model(
                        WhisperModel, backend_name, model_path, fallback_runtime
                    )
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

    def _load_funasr_model(self, settings: dict[str, Any], model_name: str):
        self._enable_system_certificate_store()
        try:
            from funasr import AutoModel
        except Exception as exc:
            raise TranscriptionError(
                "This build does not include the FunASR engine that SenseVoice and Fun-ASR need. Use Voz or a Whisper model."
                if getattr(sys, "frozen", False)
                else "The FunASR engine is not installed. Install the optional FunASR requirements, then prepare the selected speech model again."
            ) from exc

        self.runtime_message = ""
        self._configure_funasr_cache(settings)
        info = self.model_manager.selected_info()
        runtime = self._resolve_funasr_runtime_options(settings)
        model_path = str(Path(str(settings.get("model_path"))).expanduser())
        key = (
            "funasr",
            info.backend_name,
            model_path,
            runtime.device,
            runtime.compute_type,
            runtime.cpu_threads,
        )

        if self._model is not None and self._loaded_key == key:
            return self._model

        try:
            self._model = self._create_funasr_model(AutoModel, info, runtime)
            self._loaded_key = key
            return self._model
        except Exception as exc:
            if runtime.device == "cuda" and bool(settings.get("fallback_to_cpu", True)):
                fallback_runtime = RuntimeOptions(
                    device="cpu",
                    compute_type="torch",
                    cpu_threads=self._parse_cpu_threads(settings),
                )
                fallback_key = (
                    "funasr",
                    info.backend_name,
                    model_path,
                    fallback_runtime.device,
                    fallback_runtime.compute_type,
                    fallback_runtime.cpu_threads,
                )
                try:
                    self._model = self._create_funasr_model(
                        AutoModel, info, fallback_runtime
                    )
                    self._loaded_key = fallback_key
                    self.runtime_message = (
                        "GPU loading failed, so FunASR continued on CPU."
                    )
                    return self._model
                except Exception as fallback_exc:
                    raise TranscriptionError(
                        "The selected FunASR model could not load on GPU or CPU. Try SenseVoice Small first, or check disk space and model folder permissions."
                    ) from fallback_exc

            if runtime.device == "cuda":
                raise TranscriptionError(
                    "The selected FunASR model could not load on GPU. Turn on CPU fallback or select CPU only."
                ) from exc
            raise TranscriptionError(
                "The selected FunASR model could not be loaded or downloaded. Install the optional FunASR requirements and check internet, disk space, and model folder permissions."
            ) from exc

    def _create_model(
        self,
        whisper_model_class: Any,
        backend_name: str,
        model_path: str,
        runtime: RuntimeOptions,
    ):
        kwargs: dict[str, Any] = {
            "device": runtime.device,
            "compute_type": runtime.compute_type,
            "download_root": model_path,
            "local_files_only": False,
        }
        if runtime.device == "cpu":
            # faster-whisper's own default is 4 threads, a fraction of most CPUs.
            kwargs["cpu_threads"] = runtime.cpu_threads or default_cpu_threads()
        return whisper_model_class(backend_name, **kwargs)

    def _create_funasr_model(
        self,
        automodel_class: Any,
        info: Any,
        runtime: RuntimeOptions,
    ):
        kwargs: dict[str, Any] = {
            "model": info.backend_name,
            "device": "cuda:0" if runtime.device == "cuda" else "cpu",
            "disable_update": True,
            "disable_pbar": True,
            "disable_log": True,
        }
        if runtime.device == "cpu":
            kwargs["ncpu"] = runtime.cpu_threads or default_cpu_threads()
        if info.hub:
            kwargs["hub"] = info.hub
        if info.trust_remote_code:
            kwargs["trust_remote_code"] = True
        if info.vad_model:
            kwargs["vad_model"] = info.vad_model
            kwargs["vad_kwargs"] = {"max_single_segment_time": 30000}
        if info.punc_model:
            kwargs["punc_model"] = info.punc_model
        return automodel_class(**kwargs)

    def _should_retry_on_cpu(self, settings: dict[str, Any]) -> bool:
        return (
            bool(settings.get("fallback_to_cpu", True))
            and self._loaded_key is not None
            and len(self._loaded_key) >= 4
            and self._loaded_key[0] == "whisper"
            and self._loaded_key[3] == "cuda"
        )

    def _should_retry_funasr_on_cpu(self, settings: dict[str, Any]) -> bool:
        return (
            bool(settings.get("fallback_to_cpu", True))
            and self._loaded_key is not None
            and len(self._loaded_key) >= 4
            and self._loaded_key[0] == "funasr"
            and self._loaded_key[3] == "cuda"
        )

    def _load_cpu_fallback_model(self, settings: dict[str, Any]):
        from faster_whisper import WhisperModel

        model_name = self.model_manager.selected_model()
        backend_name = self.model_manager.backend_model_name(model_name)
        model_path = str(Path(str(settings.get("model_path"))).expanduser())
        fallback_runtime = RuntimeOptions(
            device="cpu",
            compute_type="int8",
            cpu_threads=self._parse_cpu_threads(settings),
        )
        fallback_key = (
            "whisper",
            backend_name,
            model_path,
            fallback_runtime.device,
            fallback_runtime.compute_type,
            fallback_runtime.cpu_threads,
        )
        self._model = self._create_model(
            WhisperModel, backend_name, model_path, fallback_runtime
        )
        self._loaded_key = fallback_key
        self.runtime_message = "GPU transcription failed, so Whisper Anywhere continued on CPU with safe int8 precision."
        return self._model

    def _load_funasr_cpu_fallback_model(self, settings: dict[str, Any]):
        self._enable_system_certificate_store()
        from funasr import AutoModel

        self._configure_funasr_cache(settings)
        info = self.model_manager.selected_info()
        model_path = str(Path(str(settings.get("model_path"))).expanduser())
        fallback_runtime = RuntimeOptions(
            device="cpu",
            compute_type="torch",
            cpu_threads=self._parse_cpu_threads(settings),
        )
        fallback_key = (
            "funasr",
            info.backend_name,
            model_path,
            fallback_runtime.device,
            fallback_runtime.compute_type,
            fallback_runtime.cpu_threads,
        )
        self._model = self._create_funasr_model(AutoModel, info, fallback_runtime)
        self._loaded_key = fallback_key
        self.runtime_message = "GPU transcription failed, so FunASR continued on CPU."
        return self._model

    def _resolve_runtime_options(self, settings: dict[str, Any]) -> RuntimeOptions:
        messages: list[str] = []
        device = self._resolve_device(settings, messages)
        compute_type = self._resolve_compute_type(device, settings, messages)
        cpu_threads = self._parse_cpu_threads(settings) if device == "cpu" else None
        self.runtime_message = " ".join(messages).strip()
        return RuntimeOptions(
            device=device, compute_type=compute_type, cpu_threads=cpu_threads
        )

    def _resolve_funasr_runtime_options(
        self, settings: dict[str, Any]
    ) -> RuntimeOptions:
        messages: list[str] = []
        device = self._resolve_funasr_device(settings, messages)
        cpu_threads = self._parse_cpu_threads(settings) if device == "cpu" else None
        self.runtime_message = " ".join(messages).strip()
        return RuntimeOptions(
            device=device, compute_type="torch", cpu_threads=cpu_threads
        )

    def _resolve_funasr_device(
        self, settings: dict[str, Any], messages: list[str]
    ) -> str:
        if (
            bool(settings.get("low_ram_mode", False))
            or str(settings.get("performance_preset", "balanced")) == "low_ram"
        ):
            return "cpu"

        requested = str(settings.get("device", "auto"))
        use_gpu_if_available = bool(settings.get("use_gpu_if_available", True))
        fallback_to_cpu = bool(settings.get("fallback_to_cpu", True))

        if requested == "cpu":
            return "cpu"

        if requested == "gpu" and not use_gpu_if_available:
            messages.append("GPU is disabled in settings, so CPU was used.")
            return "cpu"

        cuda_available = self._torch_cuda_available()
        if requested == "gpu":
            if cuda_available:
                return "cuda"
            if fallback_to_cpu:
                messages.append(
                    "A CUDA-capable PyTorch setup was not found, so CPU was used."
                )
                return "cpu"
            raise TranscriptionError(
                "GPU preferred is selected, but PyTorch cannot see a CUDA GPU. Turn on CPU fallback or choose CPU only."
            )

        if requested == "auto" and use_gpu_if_available and cuda_available:
            return "cuda"
        return "cpu"

    def _resolve_device(self, settings: dict[str, Any], messages: list[str]) -> str:
        if (
            bool(settings.get("low_ram_mode", False))
            or str(settings.get("performance_preset", "balanced")) == "low_ram"
        ):
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
            raise TranscriptionError(
                "GPU preferred is selected, but a compatible CUDA GPU was not found. Turn on CPU fallback or choose CPU only."
            )

        if requested == "auto" and use_gpu_if_available and cuda_available:
            return "cuda"
        return "cpu"

    def _resolve_compute_type(
        self, device: str, settings: dict[str, Any], messages: list[str]
    ) -> str:
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

        fallback_order = (
            ["int8", "float32"]
            if device == "cpu"
            else ["float16", "int8_float16", "float32", "int8"]
        )
        for candidate in fallback_order:
            if candidate in supported:
                messages.append(
                    f"Compute precision {requested} is not supported on {device.upper()}, so {candidate} was used."
                )
                return candidate

        safe = "int8" if device == "cpu" else "float16"
        messages.append(
            f"Compute precision {requested} may not be supported, so {safe} was selected as a safe default."
        )
        return safe

    def _parse_cpu_threads(self, settings: dict[str, Any]) -> int | None:
        raw_threads = settings.get("cpu_threads", "auto")
        if raw_threads == "auto":
            return None
        try:
            return max(1, min(64, int(raw_threads)))
        except (TypeError, ValueError):
            return None

    def _configure_funasr_cache(self, settings: dict[str, Any]) -> None:
        cache_root = Path(str(settings.get("model_path"))).expanduser() / "funasr"
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        os.environ.setdefault("MODELSCOPE_CACHE", str(cache_root / "modelscope"))
        os.environ.setdefault("HF_HOME", str(cache_root / "huggingface"))

    def _enable_system_certificate_store(self) -> None:
        try:
            import truststore

            truststore.inject_into_ssl()
        except Exception:
            pass

    def _funasr_generate_kwargs(
        self, audio_path: str, settings: dict[str, Any]
    ) -> dict[str, Any]:
        model_name = self.model_manager.selected_model()
        input_value: str | list[str] = (
            [audio_path] if model_name == "funasr-nano" else audio_path
        )
        kwargs: dict[str, Any] = {
            "input": input_value,
            "cache": {},
            "use_itn": True,
            "batch_size_s": 60,
            "disable_pbar": True,
            "disable_log": True,
        }
        if model_name == "funasr-nano":
            kwargs["batch_size_s"] = 0
        else:
            kwargs["merge_vad"] = True
            kwargs["merge_length_s"] = 15
            kwargs["language"] = self._funasr_language(settings)
        return kwargs

    def _funasr_language(self, settings: dict[str, Any]) -> str:
        language_mode = str(settings.get("language_mode", "auto"))
        forced_language = str(settings.get("forced_language", "")).strip().lower()
        if language_mode != "force" or not forced_language:
            return "auto"
        language_aliases = {
            "zh": "zn",
            "zh-cn": "zn",
            "cmn": "zn",
            "mandarin": "zn",
            "jp": "ja",
            "jpn": "ja",
            "kr": "ko",
            "kor": "ko",
        }
        return language_aliases.get(forced_language, forced_language)

    def _extract_funasr_text(self, result: Any) -> str:
        if isinstance(result, dict):
            entries = [result]
        elif isinstance(result, list):
            entries = result
        else:
            return self._postprocess_funasr_text(str(result or ""))

        parts: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                text = self._postprocess_funasr_text(str(entry or ""))
                if text:
                    parts.append(text)
                continue

            sentences = entry.get("sentence_info")
            if isinstance(sentences, list) and sentences:
                for sentence in sentences:
                    if isinstance(sentence, dict):
                        text = self._postprocess_funasr_text(
                            str(sentence.get("text") or "")
                        )
                        if text:
                            parts.append(text)
                continue

            text = self._postprocess_funasr_text(str(entry.get("text") or ""))
            if text:
                parts.append(text)
        return " ".join(parts).strip()

    def _postprocess_funasr_text(self, text: str) -> str:
        try:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            return str(rich_transcription_postprocess(text)).strip()
        except Exception:
            return re.sub(r"<\|[^|]+?\|>", "", text).strip()

    def _append_runtime_message(self, message: str) -> None:
        if not message:
            return
        if self.runtime_message:
            self.runtime_message = f"{self.runtime_message} {message}"
        else:
            self.runtime_message = message

    def _torch_cuda_available(self) -> bool:
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:
            return False

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
