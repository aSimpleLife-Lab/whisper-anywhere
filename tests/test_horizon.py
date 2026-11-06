from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never a real window

import pytest
from PySide6.QtWidgets import QApplication

import ui.horizon as horizon
from ui.horizon import (
    MAX_VOICE_VOXELS,
    SHIFT_SECONDS,
    VOICE_ROWS,
    VoiceHorizon,
    band_range,
    terrain_height,
    voice_row,
)


@pytest.fixture
def clock(monkeypatch) -> SimpleNamespace:
    now = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(horizon, "time", SimpleNamespace(monotonic=lambda: now.value, localtime=time.localtime))
    return now


@pytest.fixture
def scene(clock) -> VoiceHorizon:
    _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    widget = VoiceHorizon()
    widget.resize(404, 216)
    widget.grab()  # the first paint sets the column count
    return widget


def roll(scene: VoiceHorizon, clock: SimpleNamespace, shifts: int, level: float = 0.0) -> None:
    """Timer ticks far enough apart that every one rolls the equaliser back a row."""
    for _ in range(shifts):
        scene.push_level(level)
        clock.value += SHIFT_SECONDS
        scene._tick()


def test_voice_row_rises_with_the_voice_inside_the_band() -> None:
    columns = 31
    band = band_range(columns)

    quiet = voice_row(columns, 0.0, 1.0)
    loud = voice_row(columns, 0.8, 1.0)

    assert quiet == [0] * columns
    assert max(loud) > 2
    assert max(loud) <= MAX_VOICE_VOXELS
    assert all(height == 0 for column, height in enumerate(loud) if column not in band)


def test_the_equaliser_stage_is_flat() -> None:
    columns = 31
    for row in range(VOICE_ROWS):
        for column in band_range(columns):
            assert terrain_height(column, row, columns) == 1


def test_after_an_error_the_scene_slows_back_down(scene, clock) -> None:
    scene.set_animated(True)
    scene.set_mode("busy")
    roll(scene, clock, VOICE_ROWS)  # the cyan scanner fills the stage
    scene.set_mode("error")
    assert scene._timer.interval() == 33  # the last rows still roll out

    roll(scene, clock, VOICE_ROWS)
    assert scene._timer.interval() == 90  # then only the clouds drift, as when idle
    scene.set_window_active(False)
    assert scene._timer.interval() == 1000


def test_an_error_behind_another_window_drops_to_one_frame_a_second(scene, clock) -> None:
    scene.set_animated(True)
    scene.set_window_active(False)
    scene.set_mode("listening")
    roll(scene, clock, VOICE_ROWS, level=0.8)
    scene.set_mode("error")
    roll(scene, clock, VOICE_ROWS)

    assert scene._timer.isActive()
    assert scene._timer.interval() == 1000


def test_frames_between_shifts_reuse_the_drawn_columns(scene, clock, monkeypatch) -> None:
    scene.set_mode("listening")
    roll(scene, clock, VOICE_ROWS, level=0.9)
    drawn: list[tuple] = []
    cuboid = scene._cuboid
    monkeypatch.setattr(scene, "_cuboid", lambda *args: (drawn.append(args), cuboid(*args)))

    scene.grab()
    once = len(drawn)
    scene.grab()
    scene.grab()  # the rows only move every SHIFT_SECONDS; the frames between redraw nothing
    assert once and len(drawn) == once

    scene.set_mode("busy")  # the same rows turn cyan at once
    scene.grab()
    assert len(drawn) == 2 * once
    roll(scene, clock, 1)
    scene.grab()
    assert len(drawn) > 2 * once
