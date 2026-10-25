from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from core import voz_engine
from core.settings_manager import SettingsManager


@dataclass(frozen=True)
class SpeechModelInfo:
    name: str
    backend_name: str
    approximate_size: str
    speed_estimate: str
    accuracy_estimate: str
    resource_usage_estimate: str
    recommended_use: str
    engine: str = "whisper"
    hub: str = ""
    trust_remote_code: bool = False
    vad_model: str = ""
    punc_model: str = ""
    display_name: str = ""
    languages: str = "99 languages"

    @property
    def label(self) -> str:
        return self.display_name or self.name


WhisperModelInfo = SpeechModelInfo


MODEL_ORDER = [
    "voz",
    "tiny",
    "base",
    "small",
    "medium",
    "large",
    "large-v2",
    "large-v3",
    "turbo",
    "sensevoice-small",
    "funasr-nano",
]

MODEL_INFO: dict[str, SpeechModelInfo] = {
    "voz": SpeechModelInfo(
        "voz",
        voz_engine.REPO_ID,
        f"{voz_engine.DOWNLOAD_MB} MB",
        "Fastest",
        "Highest",
        "~0.7 GB RAM, CPU",
        "Fast, accurate dictation with punctuation",
        engine="voz",
        display_name="Voz",
        languages="25 European languages",
    ),
    "tiny": SpeechModelInfo(
        "tiny",
        "tiny",
        "39 MB",
        "Fastest",
        "Lowest",
        "Very low RAM",
        "Quick notes on slower PCs",
        display_name="Whisper Tiny",
    ),
    "base": SpeechModelInfo(
        "base",
        "base",
        "74 MB",
        "Fast",
        "Basic",
        "Low RAM",
        "Default balanced starter",
        display_name="Whisper Base",
    ),
    "small": SpeechModelInfo(
        "small",
        "small",
        "244 MB",
        "Balanced",
        "Good",
        "Moderate RAM",
        "General voice typing",
        display_name="Whisper Small",
    ),
    "medium": SpeechModelInfo(
        "medium",
        "medium",
        "769 MB",
        "Moderate",
        "Very good",
        "Higher RAM",
        "Better dictation quality",
        display_name="Whisper Medium",
    ),
    "large": SpeechModelInfo(
        "large",
        "large",
        "1550 MB",
        "Slow",
        "High",
        "High RAM/VRAM",
        "Accuracy over speed",
        display_name="Whisper Large",
    ),
    "large-v2": SpeechModelInfo(
        "large-v2",
        "large-v2",
        "1550 MB",
        "Slow",
        "Higher",
        "High RAM/VRAM",
        "Difficult audio",
        display_name="Whisper Large v2",
    ),
    "large-v3": SpeechModelInfo(
        "large-v3",
        "large-v3",
        "1550 MB",
        "Slow",
        "Highest",
        "High RAM/VRAM",
        "Best local quality",
        display_name="Whisper Large v3",
    ),
    "turbo": SpeechModelInfo(
        "turbo",
        "large-v3-turbo",
        "809 MB",
        "Fast",
        "High",
        "Moderate VRAM",
        "Fast high-quality dictation",
        display_name="Whisper Turbo",
    ),
    "sensevoice-small": SpeechModelInfo(
        "sensevoice-small",
        "FunAudioLLM/SenseVoiceSmall",
        "234 MB",
        "Very fast",
        "High",
        "Moderate RAM",
        "FunASR multilingual dictation",
        engine="funasr",
        hub="hf",
        vad_model="fsmn-vad",
        display_name="SenseVoice Small",
        languages="zh, en, yue, ja, ko",
    ),
    "funasr-nano": SpeechModelInfo(
        "funasr-nano",
        "FunAudioLLM/Fun-ASR-Nano-2512",
        "800 MB",
        "Very fast",
        "High",
        "Higher RAM/VRAM",
        "FunASR wide multilingual dictation",
        engine="funasr",
        hub="hf",
        trust_remote_code=True,
        vad_model="fsmn-vad",
        display_name="Fun-ASR Nano",
        languages="31 languages",
    ),
}

MODEL_ALIASES = {
    "Voz": "voz",
    "desert-ant-labs/voz": "voz",
    "large-v3-turbo": "turbo",
    "SenseVoiceSmall": "sensevoice-small",
    "iic/SenseVoiceSmall": "sensevoice-small",
    "fun-asr-nano": "funasr-nano",
    "Fun-ASR-Nano": "funasr-nano",
    "Fun-ASR-Nano-2512": "funasr-nano",
    "FunAudioLLM/Fun-ASR-Nano-2512": "funasr-nano",
}

LARGE_MODELS = {"large", "large-v2", "large-v3", "funasr-nano"}


class ModelManager:
    def __init__(self, settings_manager: SettingsManager) -> None:
        self.settings_manager = settings_manager

    def models(self) -> list[SpeechModelInfo]:
        return [MODEL_INFO[name] for name in MODEL_ORDER]

    def normalize_model_name(self, model_name: str) -> str:
        return MODEL_ALIASES.get(model_name, model_name)

    def selected_model(self) -> str:
        selected = self.normalize_model_name(
            str(self.settings_manager.get("selected_model", "voz"))
        )
        if selected not in MODEL_INFO:
            selected = "voz"
            self.settings_manager.set("selected_model", selected)
        elif selected != self.settings_manager.get("selected_model"):
            self.settings_manager.set("selected_model", selected)
        return selected

    def selected_info(self) -> SpeechModelInfo:
        return MODEL_INFO[self.selected_model()]

    def select_model(self, model_name: str) -> None:
        normalized = self.normalize_model_name(model_name)
        if normalized not in MODEL_INFO:
            raise ValueError(f"Unknown speech model: {model_name}")
        self.settings_manager.set("selected_model", normalized)

    def backend_model_name(self, model_name: str | None = None) -> str:
        selected = self.normalize_model_name(model_name or self.selected_model())
        return MODEL_INFO[selected].backend_name

    def model_engine(self, model_name: str | None = None) -> str:
        selected = self.normalize_model_name(model_name or self.selected_model())
        return MODEL_INFO[selected].engine

    def is_large_model(self, model_name: str | None = None) -> bool:
        return (
            self.normalize_model_name(model_name or self.selected_model())
            in LARGE_MODELS
        )

    def model_storage_path(self) -> Path:
        path = Path(str(self.settings_manager.get("model_path"))).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def marker_path(self, model_name: str | None = None) -> Path:
        selected = self.normalize_model_name(model_name or self.selected_model())
        safe_name = selected.replace("/", "_").replace("\\", "_")
        return self.model_storage_path() / f".{safe_name}.ready.json"

    def is_model_ready(self, model_name: str | None = None) -> bool:
        selected = self.normalize_model_name(model_name or self.selected_model())
        if MODEL_INFO[selected].engine == "voz":
            # Voz's files are checked directly, so a bundle copied in by hand
            # (or onto another drive through a folder link) counts as installed.
            return not voz_engine.missing_files(self.model_storage_path())
        return self.marker_path(selected).exists()

    def model_status_label(self, model_name: str | None = None) -> str:
        return "Installed" if self.is_model_ready(model_name) else "Download"

    def mark_model_ready(self, model_name: str | None = None) -> None:
        selected = self.normalize_model_name(model_name or self.selected_model())
        info = MODEL_INFO[selected]
        marker = self.marker_path(selected)
        with marker.open("w", encoding="utf-8") as file:
            json.dump({"model": asdict(info)}, file, indent=2)
            file.write("\n")
