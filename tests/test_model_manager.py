from __future__ import annotations

from core.model_manager import MODEL_INFO, MODEL_ORDER, ModelManager


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value) -> None:
        self[key] = value


def test_sizes_are_what_each_model_downloads() -> None:
    # SW-5: parameter counts were shown as megabytes, 2-4x too small. These are
    # the files each engine fetches, from the Hugging Face API (decimal MB).
    sizes = {name: MODEL_INFO[name].approximate_size for name in MODEL_ORDER if name != "voz"}

    assert sizes == {
        "tiny": "78 MB",
        "base": "148 MB",
        "small": "486 MB",
        "medium": "1531 MB",
        "large-v2": "3090 MB",
        "large-v3": "3091 MB",
        "turbo": "1622 MB",
        "sensevoice-small": "948 MB",  # with the 4 MB fsmn-vad model it needs
        "funasr-nano": "1993 MB",
    }


def test_large_is_large_v3_once_in_the_list() -> None:
    # faster-whisper downloads the same repo for both; the list showed it twice
    # with two different ratings.
    settings = FakeSettings(selected_model="large")
    manager = ModelManager(settings)

    assert "large" not in MODEL_ORDER
    assert manager.selected_model() == "large-v3"
    assert settings["selected_model"] == "large-v3"
    assert manager.backend_model_name("large") == "large-v3"
    assert manager.is_large_model("large")
