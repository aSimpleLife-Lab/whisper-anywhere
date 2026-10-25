from __future__ import annotations

from ui.horizon import MAX_VOICE_VOXELS, VOICE_ROWS, band_range, terrain_height, voice_row


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
