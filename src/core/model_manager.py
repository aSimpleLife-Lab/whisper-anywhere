from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.settings_manager import SettingsManager


@dataclass(frozen=True)
class WhisperModelInfo:
    name: str
    backend_name: str
    approximate_size: str
    speed: str
    accuracy: str
    recommended_use: str


MODEL_ORDER = [
    "tiny",
    "base",
    "small",
    "medium",
    "large",
    "large-v2",
    "large-v3",
    "turbo",
]

MODEL_INFO: dict[str, WhisperModelInfo] = {
    "tiny": WhisperModelInfo("tiny", "tiny", "39 MB", "Fastest", "Least accurate", "Quick notes on slower PCs"),
    "base": WhisperModelInfo("base", "base", "74 MB", "Fast", "Basic", "Short everyday dictation"),
    "small": WhisperModelInfo("small", "small", "244 MB", "Balanced", "Good", "General voice typing"),
    "medium": WhisperModelInfo("medium", "medium", "769 MB", "Moderate", "Very good", "Recommended V1 default"),
    "large": WhisperModelInfo("large", "large", "1550 MB", "Slow", "High", "Accuracy over speed"),
    "large-v2": WhisperModelInfo("large-v2", "large-v2", "1550 MB", "Slow", "Higher", "Difficult audio"),
    "large-v3": WhisperModelInfo("large-v3", "large-v3", "1550 MB", "Slow", "Highest", "Best quality local transcription"),
    "turbo": WhisperModelInfo("turbo", "large-v3-turbo", "809 MB", "Fast", "High", "Fast high-quality dictation"),
}


class ModelManager:
    def __init__(self, settings_manager: SettingsManager) -> None:
        self.settings_manager = settings_manager

    def models(self) -> list[WhisperModelInfo]:
        return [MODEL_INFO[name] for name in MODEL_ORDER]

    def selected_model(self) -> str:
        selected = str(self.settings_manager.get("selected_model", "medium"))
        if selected not in MODEL_INFO:
            selected = "medium"
            self.settings_manager.set("selected_model", selected)
        return selected

    def selected_info(self) -> WhisperModelInfo:
        return MODEL_INFO[self.selected_model()]

    def select_model(self, model_name: str) -> None:
        if model_name not in MODEL_INFO:
            raise ValueError(f"Unknown Whisper model: {model_name}")
        self.settings_manager.set("selected_model", model_name)

    def backend_model_name(self) -> str:
        return self.selected_info().backend_name

    def model_storage_path(self) -> Path:
        path = Path(str(self.settings_manager.get("model_path"))).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
