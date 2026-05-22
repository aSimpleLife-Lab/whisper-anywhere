from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from core.settings_manager import SettingsManager


@dataclass(frozen=True)
class WhisperModelInfo:
    name: str
    backend_name: str
    approximate_size: str
    speed_estimate: str
    accuracy_estimate: str
    resource_usage_estimate: str
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
    "tiny": WhisperModelInfo(
        "tiny",
        "tiny",
        "39 MB",
        "Fastest",
        "Lowest",
        "Very low RAM",
        "Quick notes on slower PCs",
    ),
    "base": WhisperModelInfo(
        "base", "base", "74 MB", "Fast", "Basic", "Low RAM", "Default balanced starter"
    ),
    "small": WhisperModelInfo(
        "small",
        "small",
        "244 MB",
        "Balanced",
        "Good",
        "Moderate RAM",
        "General voice typing",
    ),
    "medium": WhisperModelInfo(
        "medium",
        "medium",
        "769 MB",
        "Moderate",
        "Very good",
        "Higher RAM",
        "Better dictation quality",
    ),
    "large": WhisperModelInfo(
        "large",
        "large",
        "1550 MB",
        "Slow",
        "High",
        "High RAM/VRAM",
        "Accuracy over speed",
    ),
    "large-v2": WhisperModelInfo(
        "large-v2",
        "large-v2",
        "1550 MB",
        "Slow",
        "Higher",
        "High RAM/VRAM",
        "Difficult audio",
    ),
    "large-v3": WhisperModelInfo(
        "large-v3",
        "large-v3",
        "1550 MB",
        "Slow",
        "Highest",
        "High RAM/VRAM",
        "Best local quality",
    ),
    "turbo": WhisperModelInfo(
        "turbo",
        "large-v3-turbo",
        "809 MB",
        "Fast",
        "High",
        "Moderate VRAM",
        "Fast high-quality dictation",
    ),
}

MODEL_ALIASES = {
    "large-v3-turbo": "turbo",
}

LARGE_MODELS = {"large", "large-v2", "large-v3"}


class ModelManager:
    def __init__(self, settings_manager: SettingsManager) -> None:
        self.settings_manager = settings_manager

    def models(self) -> list[WhisperModelInfo]:
        return [MODEL_INFO[name] for name in MODEL_ORDER]

    def normalize_model_name(self, model_name: str) -> str:
        return MODEL_ALIASES.get(model_name, model_name)

    def selected_model(self) -> str:
        selected = self.normalize_model_name(
            str(self.settings_manager.get("selected_model", "base"))
        )
        if selected not in MODEL_INFO:
            selected = "base"
            self.settings_manager.set("selected_model", selected)
        elif selected != self.settings_manager.get("selected_model"):
            self.settings_manager.set("selected_model", selected)
        return selected

    def selected_info(self) -> WhisperModelInfo:
        return MODEL_INFO[self.selected_model()]

    def select_model(self, model_name: str) -> None:
        normalized = self.normalize_model_name(model_name)
        if normalized not in MODEL_INFO:
            raise ValueError(f"Unknown Whisper model: {model_name}")
        self.settings_manager.set("selected_model", normalized)

    def backend_model_name(self, model_name: str | None = None) -> str:
        selected = self.normalize_model_name(model_name or self.selected_model())
        return MODEL_INFO[selected].backend_name

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
        return self.marker_path(model_name).exists()

    def model_status_label(self, model_name: str | None = None) -> str:
        return "Installed" if self.is_model_ready(model_name) else "Download"

    def mark_model_ready(self, model_name: str | None = None) -> None:
        selected = self.normalize_model_name(model_name or self.selected_model())
        info = MODEL_INFO[selected]
        marker = self.marker_path(selected)
        with marker.open("w", encoding="utf-8") as file:
            json.dump({"model": asdict(info)}, file, indent=2)
            file.write("\n")
