from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Callable
import wave

import numpy as np

from core.model_manager import MODEL_INFO, ModelManager

SAMPLE_RATE = 16000
# Below both of these the clip is microphone hiss, not speech.
SILENCE_PEAK = 300 / 32768
SILENCE_MEAN = 25 / 32768
# huggingface_hub reads these once, when it is imported: Whisper and FunASR
# file requests give up after 30 s of silence, as Voz's downloads do.
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")

Audio = np.ndarray | str


class TranscriptionError(RuntimeError):
    pass


def _failure_reason(exc: BaseException) -> str:
    """The underlying cause in words, so a failed download says why instead of a generic hint."""
    root, depth = exc, 0
    while (root.__cause__ or root.__context__) is not None and depth < 10:
        root, depth = root.__cause__ or root.__context__, depth + 1
    if isinstance(root, OSError):
        from core.voz_engine import describe_download_error

        return describe_download_error(root)
    return f"({type(root).__name__}: {root}) Check your internet connection, disk space, and model folder permissions."


def _local_copy(download: Callable[..., str], repo: str, cache_dir: str, ready: bool) -> str:
    """The Hugging Face repo's folder in the model folder, downloaded first if need be.

    Called outside the hook pause: a download can take minutes, and a stalled
    proxy must not leave the shortcut dead. A ready model never asks huggingface.co.
    """
    if ready:
        try:
            return download(repo, cache_dir=cache_dir, local_files_only=True)
        except FileNotFoundError:  # huggingface_hub: the cached copy was deleted
            pass
    return download(repo, cache_dir=cache_dir)


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
        # A GPU that failed once (say, no cuBLAS) fails every time: CPU until
        # the settings change, which builds a new Transcriber.
        self._gpu_failed = False
        # A background warm-up and a dictation never load the model twice.
        self._lock = threading.RLock()
        # main.py points this at the hotkey listener's pause: a model load holds
        # Python's GIL for seconds, and Windows input must not wait on our hooks.
        self.load_guard = nullcontext
        # Models that loaded once: they load offline even if their marker
        # file could not be written.
        self._ready: set[str] = set()

    def is_selected_model_ready(self) -> bool:
        return self.model_manager.is_model_ready()

    def is_loaded(self) -> bool:
        return self._model is not None

    def prepare_selected_model(
        self, settings: dict[str, Any], model_name: str | None = None
    ) -> None:
        """Download if need be, then load and warm up: the background warm-up.

        `model_name` pins the model for the whole operation; the selection can
        change while it runs."""
        model_name = model_name or self.model_manager.selected_model()
        with self._lock:
            try:
                self._load_model(settings, model_name, warm_up=True)
            except Exception:
                self.unload()
                raise
            if settings.get("unload_model_after") == "each":
                self.unload()

    def transcribe(
        self, audio: Audio, settings: dict[str, Any], model_name: str | None = None
    ) -> str:
        """Transcribe 16 kHz mono float32 samples (or a WAV path) with
        `model_name`, by default the model selected when the call starts."""
        model_name = model_name or self.model_manager.selected_model()
        with self._lock:
            if self._looks_like_silence(audio):
                # The model stays loaded: an accidental tap of the shortcut
                # should not make the next real dictation reload it.
                self.runtime_message = (
                    "The recording was too short."  # a tap that ended before any audio
                    if isinstance(audio, np.ndarray) and audio.size == 0
                    else "No speech was detected above the microphone noise floor."
                )
                return ""
            try:
                engine = self.model_manager.model_engine(model_name)
                if engine == "voz":
                    text = self._transcribe_with_voz(audio, settings, model_name)
                elif engine == "funasr":
                    text = self._transcribe_with_funasr(audio, settings, model_name)
                else:
                    text = self._transcribe_with_whisper(audio, settings, model_name)
                return " ".join(text.split())
            except Exception:
                self.unload()
                raise
            finally:
                # Idle minutes are counted by the window's timer, which calls unload().
                if settings.get("unload_model_after") == "each":
                    self.unload()

    def unload(self) -> None:
        """Free the model. Waits for a running transcription instead of
        pulling the model from under it."""
        with self._lock:
            if self._model is None:
                return
            # Freeing a model holds the GIL (Voz: 70-99 ms), and Windows holds
            # back every click and key press while a hook cannot answer: the
            # hooks step aside, as for a load.
            with self.load_guard():
                self._model = None
                self._loaded_key = None

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

    def _transcribe_with_voz(
        self, audio: Audio, settings: dict[str, Any], model_name: str
    ) -> str:
        model = self._load_model(settings, model_name)
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

    def _load_voz_model(self, settings: dict[str, Any], warm_up: bool):
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
        except voz_engine.VozError as exc:
            raise TranscriptionError(
                f"Voz could not download. {exc} You can also download {', '.join(voz_engine.BUNDLE_FILES)} "
                f"in your browser from {voz_engine.HF_FILES_URL} and put them in {voz_engine.bundle_path(model_path)}."
            ) from exc
        try:
            with self.load_guard():
                model = voz_engine.VozModel(model_path, cpu_threads=threads)
            if warm_up:  # a dictation's own clip warms it up
                model.warm_up()
        except Exception as exc:
            raise TranscriptionError(
                f"Voz could not be loaded ({type(exc).__name__}: {exc}). Check disk space and the model folder."
            ) from exc
        self._model = model
        self._loaded_key = key
        return model

    def _transcribe_with_whisper(
        self, audio: Audio, settings: dict[str, Any], model_name: str
    ) -> str:
        model = self._load_model(settings, model_name)
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
                    fallback_model = self._load_cpu_fallback_model(settings, model_name)
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

    def _transcribe_with_funasr(
        self, audio: Audio, settings: dict[str, Any], model_name: str
    ) -> str:
        model = self._load_model(settings, model_name)
        kwargs = self._funasr_generate_kwargs(audio, settings, model_name)
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
                    fallback_model = self._load_funasr_cpu_fallback_model(
                        settings, model_name
                    )
                    result = fallback_model.generate(**kwargs)
                    return self._extract_funasr_text(result)
                except Exception as fallback_exc:
                    raise TranscriptionError(
                        "FunASR transcription failed. Try CPU mode, check your microphone audio, or use a smaller speech model."
                    ) from fallback_exc
            raise TranscriptionError(
                "FunASR transcription failed. Try CPU mode, check your microphone audio, or use a smaller speech model."
            ) from exc

    def _load_model(
        self, settings: dict[str, Any], model_name: str, warm_up: bool = False
    ):
        self.runtime_message = ""  # a silent tap's "No speech" is not this dictation's
        engine = self.model_manager.model_engine(model_name)
        if engine == "voz":  # checked by its files, never by the marker
            return self._load_voz_model(settings, warm_up)
        if engine == "funasr":
            model = self._load_funasr_model(settings, model_name)
        else:
            model = self._load_whisper_model(settings, model_name)
        self._mark_ready(model_name)
        return model

    def _is_ready(self, model_name: str) -> bool:
        return model_name in self._ready or self.model_manager.is_model_ready(model_name)

    def _mark_ready(self, model_name: str) -> None:
        """After a model's first load: later loads, and the next start, skip
        huggingface.co. A marker that cannot be written never costs a dictation."""
        if model_name in self._ready:
            return
        self._ready.add(model_name)
        try:
            if not self.model_manager.is_model_ready(model_name):
                self.model_manager.mark_model_ready(model_name)
        except OSError:
            pass

    def _load_whisper_model(self, settings: dict[str, Any], model_name: str):
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
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

        ready = self._is_ready(model_name)
        if not ready:
            self._enable_system_certificate_store()  # as Voz and FunASR do: filtered networks
        try:
            model_dir = _local_copy(download_model, backend_name, model_path, ready)
        except Exception as exc:
            raise TranscriptionError(
                f"The selected Whisper model could not be downloaded. {_failure_reason(exc)}"
            ) from exc
        try:
            self._model = self._create_model(WhisperModel, model_dir, runtime)
            self._loaded_key = key
            return self._model
        except Exception as exc:
            if runtime.device == "cuda" and bool(settings.get("fallback_to_cpu", True)):
                self._gpu_failed = True
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
                        WhisperModel, model_dir, fallback_runtime
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
                f"The selected Whisper model could not be loaded or downloaded. {_failure_reason(exc)}"
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
        info = MODEL_INFO[model_name]
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
            files = self._funasr_files(info, model_name, settings)
        except Exception as exc:
            raise TranscriptionError(
                f"The selected FunASR model could not be downloaded. {_failure_reason(exc)}"
            ) from exc
        try:
            self._model = self._create_funasr_model(AutoModel, info, runtime, files)
            self._loaded_key = key
            return self._model
        except Exception as exc:
            if runtime.device == "cuda" and bool(settings.get("fallback_to_cpu", True)):
                self._gpu_failed = True
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
                        AutoModel, info, fallback_runtime, files
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
        model_dir: str,
        runtime: RuntimeOptions,
    ):
        kwargs: dict[str, Any] = {
            "device": runtime.device,
            "compute_type": runtime.compute_type,
            "local_files_only": True,  # a local folder: nothing to fetch with the hooks off
        }
        if runtime.device == "cpu":
            # faster-whisper's own default is 4 threads, a fraction of most CPUs.
            kwargs["cpu_threads"] = runtime.cpu_threads or default_cpu_threads()
        with self.load_guard():
            return whisper_model_class(model_dir, **kwargs)

    def _funasr_files(
        self, info: Any, model_name: str, settings: dict[str, Any]
    ) -> dict[str, str]:
        """Local folders for the model and its VAD and punctuation helpers.

        Left to itself, funasr fetches them inside the hook pause, with no
        timeout, into the profile's Hugging Face cache.
        """
        from funasr.download.name_maps_from_hub import name_maps_hf
        from huggingface_hub import snapshot_download

        cache_dir = self._configure_funasr_cache(settings)
        ready = self._is_ready(model_name)
        repos = {
            "model": info.backend_name,
            "vad_model": info.vad_model,
            "punc_model": info.punc_model,
        }
        return {
            key: _local_copy(snapshot_download, name_maps_hf.get(repo, repo), cache_dir, ready)
            for key, repo in repos.items()
            if repo
        }

    def _create_funasr_model(
        self,
        automodel_class: Any,
        info: Any,
        runtime: RuntimeOptions,
        files: dict[str, str],
    ):
        kwargs: dict[str, Any] = {
            "model": files["model"],
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
            kwargs["vad_model"] = files["vad_model"]
            kwargs["vad_kwargs"] = {"max_single_segment_time": 30000}
        if info.punc_model:
            kwargs["punc_model"] = files["punc_model"]
        with self.load_guard():  # local folders: nothing to fetch with the hooks off
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

    def _load_cpu_fallback_model(self, settings: dict[str, Any], model_name: str):
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model

        self._gpu_failed = True
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
        # It just loaded on the GPU, so it is on disk.
        model_dir = _local_copy(download_model, backend_name, model_path, True)
        self._model = self._create_model(WhisperModel, model_dir, fallback_runtime)
        self._loaded_key = fallback_key
        self.runtime_message = "GPU transcription failed, so Whisper Anywhere continued on CPU with safe int8 precision."
        return self._model

    def _load_funasr_cpu_fallback_model(self, settings: dict[str, Any], model_name: str):
        self._enable_system_certificate_store()
        from funasr import AutoModel

        self._gpu_failed = True
        info = MODEL_INFO[model_name]
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
        files = self._funasr_files(info, model_name, settings)
        self._model = self._create_funasr_model(AutoModel, info, fallback_runtime, files)
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
        if self._gpu_failed:
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
        # The v0.1 preset menu is gone, so its saved value no longer counts.
        if self._gpu_failed:
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
        supported = self._supported_compute_types(device)

        if requested == "auto":
            if device == "cpu":
                requested = "int8"
            elif bool(settings.get("low_vram_mode", False)):
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

    def _configure_funasr_cache(self, settings: dict[str, Any]) -> str:
        """FunASR's Hugging Face cache, in the model folder. Passed to each
        download: HF_HOME came too late, as importing funasr fixes the cache."""
        cache_root = Path(str(settings.get("model_path"))).expanduser() / "funasr"
        os.environ.setdefault("MODELSCOPE_CACHE", str(cache_root / "modelscope"))
        return str(cache_root / "huggingface" / "hub")

    def _enable_system_certificate_store(self) -> None:
        try:
            import truststore

            truststore.inject_into_ssl()
        except Exception:
            pass

    def _funasr_generate_kwargs(
        self, audio: Audio, settings: dict[str, Any], model_name: str
    ) -> dict[str, Any]:
        # Samples stay in memory: FunASR reads arrays as well as files.
        input_value: Audio | list[Audio] = [audio] if model_name == "funasr-nano" else audio
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
        language_aliases = {  # to SenseVoice's codes: it reads any code it lacks as auto
            "zh-cn": "zh",
            "cmn": "zh",
            "mandarin": "zh",
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
        # Not funasr's rich_transcription_postprocess: it turns SenseVoice's
        # event and emotion tags into emoji, "❓" for noise, and drops "The.".
        text = re.sub(r"<\|[^|]+?\|>", "", text).strip()
        return text if any(char.isalnum() for char in text) else ""

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
