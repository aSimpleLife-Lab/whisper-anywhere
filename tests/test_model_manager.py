from __future__ import annotations

from core import model_manager
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


def test_models_whose_engine_is_missing_are_left_out_of_the_list(monkeypatch) -> None:
    # The installer build has no FunASR: SenseVoice and Fun-ASR could be picked
    # there, and then only failed with "install the optional requirements".
    monkeypatch.setattr(model_manager, "engine_installed", lambda engine: engine != "funasr")

    listed = [info.name for info in ModelManager(FakeSettings(selected_model="voz")).models()]

    assert listed == ["voz", "tiny", "base", "small", "medium", "large-v2", "large-v3", "turbo"]


def test_the_selected_model_stays_in_the_list_without_its_engine(monkeypatch) -> None:
    # Settings carried over from a build with FunASR: what is selected still shows.
    monkeypatch.setattr(model_manager, "engine_installed", lambda engine: engine != "funasr")

    listed = [info.name for info in ModelManager(FakeSettings(selected_model="sensevoice-small")).models()]

    assert "sensevoice-small" in listed and "funasr-nano" not in listed


def test_funasr_counts_as_installed_only_when_it_can_be_imported(monkeypatch) -> None:
    monkeypatch.setattr(model_manager.importlib.util, "find_spec", lambda name: None)
    model_manager.engine_installed.cache_clear()
    try:
        assert not model_manager.engine_installed("funasr")
        assert model_manager.engine_installed("whisper") and model_manager.engine_installed("voz")
    finally:
        model_manager.engine_installed.cache_clear()
