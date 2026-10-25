"""Voxel Horizon: the scene box, drawn as voxel art, with a voice synthesizer.

A blocky diorama under LumaDeck's time-of-day sky (dawn, day, dusk and night
palettes; a pixel sun on its arc or the moon in its real phase; voxel clouds,
stepped mountains, voxel pines). The front rows are a 3D equaliser: speech
raises glowing columns that roll back into the distance, and while the model
works a cyan scanner sweeps along the front.

Everything is projected with one small perspective model (see `_point`), so
terrain, trees and the equaliser always line up. The sky and terrain are drawn
once into a cached pixmap; each frame only adds clouds, stars and the voice.
"""

from __future__ import annotations

import math
import time
from collections import deque
from datetime import datetime, timezone

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import QAbstractButton, QWidget

from ui.widgets import COLORS, icon_font

# Sky palettes per phase (LumaDeck): sky top, upper, horizon, bottom, far
# ridge, middle ridge, near hill, pine; then dimming shade and star opacity.
PALETTES = {
    "dawn": ("#1B2940", "#46536B", "#D18458", "#EBC38D", "#829198", "#506A70", "#203B40", "#132B30", 0.03, 0.04),
    "day": ("#1A566A", "#3E8798", "#8CC6C9", "#D3E3D2", "#7D9C9D", "#466E72", "#1D4347", "#123438", 0.00, 0.00),
    "dusk": ("#171B31", "#43354F", "#BE6848", "#DEA069", "#767B86", "#4D5965", "#292E3B", "#191F2A", 0.07, 0.10),
    "night": ("#07111C", "#102637", "#264453", "#385561", "#52666F", "#304B55", "#162B33", "#0D2228", 0.13, 0.78),
}
COLOUR_COUNT = 8
GRASS = "#78A85A"
LEAVES = "#3E7A45"
BARK = "#5A3E2B"
SYNODIC_MONTH = 29.530588853
NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
TWILIGHT_HOURS = 0.75

ROWS = 8  # depth of the voxel grid
VOICE_ROWS = 5  # front rows that carry the equaliser
SHRINK = 0.075  # perspective: each row back is this much narrower
DEPTH = 0.36  # row depth on screen, in cell widths
UNIT = 0.42  # voxel height on screen, in cell widths
MAX_VOICE_VOXELS = 7
SHIFT_SECONDS = 0.08  # the equaliser rolls back one row this often
PIXEL = 4  # pixel-art size for the sun, moon and clouds
STARS = ((0.06, 0.09), (0.24, 0.14), (0.93, 0.07), (0.15, 0.24), (0.38, 0.10), (0.84, 0.21),
         (0.31, 0.05), (0.73, 0.13), (0.96, 0.27), (0.55, 0.06), (0.64, 0.19), (0.47, 0.23))
CLOUD = ("   ####      ",
         " ######## ## ",
         "#############")


def _mix(a: str | QColor, b: str | QColor, amount: float) -> QColor:
    first, second = QColor(a), QColor(b)
    amount = max(0.0, min(1.0, amount))
    return QColor(
        round(first.red() + (second.red() - first.red()) * amount),
        round(first.green() + (second.green() - first.green()) * amount),
        round(first.blue() + (second.blue() - first.blue()) * amount),
    )


def daylight_hours(now: datetime) -> tuple[float, float]:
    """Approximate local sunrise and sunset hours without a location.

    Uses latitude 40 degrees and the centre of the local time zone, which is
    close enough to put the sun in the right part of the sky all year.
    """
    day = now.timetuple().tm_yday
    declination = math.radians(-23.44) * math.cos(2 * math.pi * (day + 10) / 365)
    latitude = math.radians(40.0)
    cos_hour = -math.tan(latitude) * math.tan(declination)
    half_day = math.degrees(math.acos(max(-1.0, min(1.0, cos_hour)))) / 15.0
    noon = 12.0 + (1.0 if time.localtime().tm_isdst > 0 else 0.0)
    return noon - half_day, noon + half_day


def scene_state(now: datetime | None = None) -> dict:
    """Palette, phase and celestial progress for a moment of the day."""
    now = now or datetime.now()
    hour = now.hour + now.minute / 60 + now.second / 3600
    sunrise, sunset = daylight_hours(now)
    edges = [
        (sunrise - TWILIGHT_HOURS, "night", "dawn"),
        (sunrise + TWILIGHT_HOURS, "dawn", "day"),
        (sunset - TWILIGHT_HOURS, "day", "dusk"),
        (sunset + TWILIGHT_HOURS, "dusk", "night"),
    ]
    phase = "night"
    for edge, _before, after in edges:
        if hour >= edge:
            phase = after
    palette = PALETTES[phase]
    for edge, before, after in edges:
        distance = hour - edge
        if abs(distance) < 0.25:  # cross-fade across the edge
            blend = (distance + 0.25) / 0.5
            palette = tuple(
                _mix(PALETTES[before][i], PALETTES[after][i], blend)
                if i < COLOUR_COUNT
                else PALETTES[before][i] + (PALETTES[after][i] - PALETTES[before][i]) * blend
                for i in range(len(PALETTES[before]))
            )
            break
    night_length = 24 - (sunset - sunrise)
    return {
        "phase": phase,
        "colours": [QColor(value) for value in palette[:COLOUR_COUNT]],
        "dimming": palette[8],
        "star_opacity": palette[9],
        "sun": (hour - sunrise) / max(0.1, sunset - sunrise),
        "moon": ((hour - sunset) % 24) / max(0.1, night_length),
        "lunar": ((now.astimezone(timezone.utc) - NEW_MOON).total_seconds() / 86400 / SYNODIC_MONTH) % 1.0,
    }


def voice_row(columns: int, level: float, elapsed: float) -> list[int]:
    """One equaliser row: voxel heights across the band for a voice level."""
    band = band_range(columns)
    heights = [0] * columns
    strength = min(1.0, level * 1.5) ** 0.65
    if strength < 0.02:
        return heights
    span = max(1, band.stop - band.start - 1)
    for column in band:
        position = (column - band.start) / span
        envelope = math.sin(math.pi * position) ** 0.8
        ripple = 0.5 + 0.5 * math.sin(position * 11 + elapsed * 7.0) * math.cos(position * 5.3 - elapsed * 3.1)
        heights[column] = round(MAX_VOICE_VOXELS * strength * envelope * (0.45 + 0.55 * ripple))
    return heights


def band_range(columns: int) -> range:
    """The equaliser uses the middle of the stage, leaving the edges for trees."""
    margin = max(2, columns // 7)
    return range(margin, columns - margin)


def terrain_height(column: int, row: int, columns: int) -> int:
    """Voxel ground: a flat stage in front, rising hills behind."""
    band = band_range(columns)
    if row < VOICE_ROWS and column in band:
        return 1
    swell = 0.5 + 0.5 * math.sin(column * 0.55 + row * 1.3 + 0.7)
    roll = 0.5 + 0.5 * math.sin(column * 0.21 - row * 0.6 + 2.1)
    rise = max(0, row - VOICE_ROWS + 2) * 0.55
    return max(1, min(7, round(1 + rise + 1.6 * swell * (row / ROWS + 0.3) + 1.4 * roll * row / ROWS)))


class VoiceHorizon(QWidget):
    """Decorative scene; it never takes input."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.mode = "idle"
        self._state = scene_state()
        self._started = time.monotonic()
        self._animated = False
        self._active = True
        self._peak = 0.0
        self._last_shift = 0.0
        self._columns = 0
        self._voice: deque[list[int]] = deque(maxlen=VOICE_ROWS)
        self._cache: QPixmap | None = None
        self._cache_key: tuple = ()
        self._cloud = self._cloud_path()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._clock = QTimer(self)
        self._clock.setInterval(60_000)
        self._clock.timeout.connect(self._refresh_sky)

    # ------------------------------------------------------------ control

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self._apply_timer()
            self.update()

    def push_level(self, level: float) -> None:
        self._peak = max(self._peak, max(0.0, min(1.0, level)))

    def set_animated(self, animated: bool) -> None:
        """Motion only while the window is on screen."""
        self._animated = animated
        if animated:
            self._refresh_sky()
            self._clock.start()
        else:
            self._clock.stop()
        self._apply_timer()

    def set_window_active(self, active: bool) -> None:
        self._active = active
        self._apply_timer()

    def _apply_timer(self) -> None:
        if not self._animated:
            self._timer.stop()
        elif self.mode in ("listening", "busy") or any(any(row) for row in self._voice):
            self._timer.start(33)  # the equaliser moves at 30 fps
        else:
            self._timer.start(90 if self._active else 1000)  # clouds drift

    def _refresh_sky(self) -> None:
        self._state = scene_state()
        self._cache_key = ()
        self.update()

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self._last_shift >= SHIFT_SECONDS and self._columns:
            self._last_shift = now
            if self.mode == "listening":
                row = voice_row(self._columns, self._peak, now - self._started)
            elif self.mode == "busy":
                row = self._scanner_row(now - self._started)
            else:
                row = [0] * self._columns
            self._voice.appendleft(row)
            self._peak *= 0.35
            if self.mode == "idle" and not any(any(r) for r in self._voice):
                self._apply_timer()
        self.update()

    def _scanner_row(self, elapsed: float) -> list[int]:
        band = band_range(self._columns)
        head = band.start + int((elapsed * 18) % max(1, len(band)))
        return [1 if 0 <= head - column < 3 else 0 for column in range(self._columns)]

    # ---------------------------------------------------------- geometry

    def _layout(self) -> tuple[float, float, float, int]:
        width, height = self.width(), self.height()
        columns = max(21, int(width / 21) | 1)
        cell = width / (columns - 4)
        return width / 2, height + cell * 0.25, cell, columns

    def _point(self, x: float, z: float, h: float) -> QPointF:
        """Grid (column x, row z, height h in voxels) to the screen."""
        cx, y0, cell, columns = self._geometry
        scale = 1 - SHRINK * z
        base = y0 - cell * DEPTH * (z - SHRINK * z * (z - 1) / 2)
        return QPointF(cx + (x - columns / 2) * cell * scale, base - h * cell * UNIT * scale)

    def _cuboid(
        self,
        painter: QPainter,
        x0: float,
        x1: float,
        z0: float,
        z1: float,
        h0: float,
        h1: float,
        top: QColor,
        front: QColor,
        side: QColor,
    ) -> None:
        p = self._point
        columns = self._geometry[3]
        painter.setBrush(front)
        painter.drawPolygon(QPolygonF([p(x0, z0, h0), p(x1, z0, h0), p(x1, z0, h1), p(x0, z0, h1)]))
        painter.setBrush(top)
        painter.drawPolygon(QPolygonF([p(x0, z0, h1), p(x1, z0, h1), p(x1, z1, h1), p(x0, z1, h1)]))
        # The face turned towards the centre of view is the one we can see.
        if x1 <= columns / 2:
            painter.setBrush(side)
            painter.drawPolygon(QPolygonF([p(x1, z0, h0), p(x1, z1, h0), p(x1, z1, h1), p(x1, z0, h1)]))
        elif x0 >= columns / 2:
            painter.setBrush(side)
            painter.drawPolygon(QPolygonF([p(x0, z0, h0), p(x0, z1, h0), p(x0, z1, h1), p(x0, z0, h1)]))

    @staticmethod
    def _order(columns: int) -> list[int]:
        """Outer columns first, so faces turned to the centre are not overdrawn."""
        middle = columns // 2
        return list(range(0, middle)) + list(range(columns - 1, middle, -1)) + [middle]

    # ------------------------------------------------------------ painting

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 8, 8)
        painter.setClipPath(clip)
        self._geometry = self._layout()
        columns = self._geometry[3]
        if columns != self._columns:
            self._columns = columns
            self._voice.clear()
        key = (self.width(), self.height(), self.devicePixelRatioF(), self._state["phase"], round(self._state["sun"], 3))
        if self._cache is None or key != self._cache_key:
            self._cache = self._render_static()
            self._cache_key = key
        painter.drawPixmap(0, 0, self._cache)
        elapsed = time.monotonic() - self._started
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self._paint_stars(painter, elapsed)
        self._paint_clouds(painter, elapsed)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_voice(painter, elapsed)
        painter.setClipping(False)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(COLORS["border"]), 1))
        painter.drawRoundedRect(rect, 8, 8)

    def _render_static(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(int(self.width() * ratio), int(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        state = self._state
        c = state["colours"]
        width, height = float(self.width()), float(self.height())
        # Banded sky, the pixel-art way.
        bands = 14
        for band in range(bands):
            top = height * band / bands
            colour = self._sample(c[:4], (band + 0.5) / bands)
            painter.fillRect(QRectF(0, top, width, height / bands + 1), colour)
        if state["phase"] == "night":
            self._paint_moon(painter, state["moon"], state["lunar"])
        else:
            self._paint_sun(painter, state["sun"])
        self._paint_mountains(painter, width, c)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        columns = self._geometry[3]
        haze = c[2]
        trees = self._tree_cells(columns)
        for row in range(ROWS - 1, -1, -1):
            fog = row / (ROWS - 1) * 0.55
            grass = _mix(_mix(c[5], GRASS, 0.5).lighter(118), haze, fog)
            for column in self._order(columns):
                ground = terrain_height(column, row, columns)
                self._cuboid(
                    painter, column, column + 1, row, row + 1, 0, ground,
                    grass, _mix(grass.darker(140), haze, fog * 0.5), _mix(grass.darker(175), haze, fog * 0.5),
                )
                if (column, row) in trees:
                    self._paint_tree(painter, column, row, ground, c, fog)
        if state["dimming"] > 0:
            painter.fillRect(QRectF(0, 0, width, height), QColor(6, 12, 16, int(255 * state["dimming"])))
        painter.end()
        return pixmap

    @staticmethod
    def _sample(colours: list[QColor], t: float) -> QColor:
        stops = (0, 0.38, 0.76, 1)
        for index in range(3):
            if t <= stops[index + 1]:
                span = stops[index + 1] - stops[index]
                return _mix(colours[index], colours[index + 1], (t - stops[index]) / span)
        return colours[-1]

    def _tree_cells(self, columns: int) -> set[tuple[int, int]]:
        band = band_range(columns)
        cells = {(1, 1), (2, 3), (0, 4), (columns - 2, 1), (columns - 3, 3), (columns - 1, 4)}
        for row in range(VOICE_ROWS, ROWS):
            for column in range(columns):
                if (column * 7 + row * 13) % 11 == 0 and column not in (band.start, band.stop - 1):
                    cells.add((column, row))
        return {(x, z) for x, z in cells if 0 <= x < columns}

    def _paint_tree(self, painter: QPainter, column: int, row: int, ground: int, c, fog: float) -> None:
        haze = c[2]
        bark = _mix(BARK, haze, fog)
        leaves = _mix(_mix(c[7], LEAVES, 0.55), haze, fog)
        x = column + 0.5
        z = row + 0.5
        self._cuboid(painter, x - 0.12, x + 0.12, z - 0.12, z + 0.12, ground, ground + 0.7,
                     bark.lighter(120), bark, bark.darker(130))
        bottom = ground + 0.6
        for width, tall in ((0.86, 0.95), (0.62, 0.85), (0.36, 0.8)):
            half = width / 2
            tier = leaves.lighter(100 + int(30 * (1 - width)))
            self._cuboid(painter, x - half, x + half, z - half, z + half, bottom, bottom + tall,
                         tier.lighter(125), tier, tier.darker(140))
            bottom += tall * 0.82

    def _paint_mountains(self, painter: QPainter, width: float, c) -> None:
        """Stepped voxel peaks far behind the grid."""
        horizon = self._point(0, ROWS, 0).y()
        step = PIXEL * 3
        for depth, colour, height_scale, phase in ((0.0, _mix(c[4], c[2], 0.55), 1.0, 0.3), (1.0, _mix(c[5], c[2], 0.4), 0.7, 1.9)):
            path = QPainterPath(QPointF(0, horizon + 2))
            x = 0.0
            while x <= width + step:
                t = x / max(width, 1.0)
                lift = (0.5 + 0.5 * math.sin(t * 7.3 + phase)) * 0.6 + (0.5 + 0.5 * math.sin(t * 17.1 + phase * 2)) * 0.4
                top = horizon - (14 + 34 * lift) * height_scale - depth * 4
                top = round(top / PIXEL) * PIXEL
                path.lineTo(x, top)
                path.lineTo(x + step, top)
                x += step
            path.lineTo(width + step, horizon + 2)
            path.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawPath(path)

    def _celestial_centre(self, progress: float) -> QPointF:
        width, height = float(self.width()), self._point(0, ROWS, 0).y()
        progress = max(0.0, min(1.0, progress))
        arc = math.sin(progress * math.pi)
        return QPointF(40 + (width - 80) * progress, height - 14 - arc * (height - 50))

    def _pixel_disc(self, painter: QPainter, centre: QPointF, radius: float, colour_at) -> None:
        reach = int(radius) + 5
        for dy in range(-reach, reach + 1):
            for dx in range(-reach, reach + 1):
                colour = colour_at(dx, dy, math.hypot(dx, dy))
                if colour is not None:
                    painter.fillRect(
                        QRectF(round(centre.x() / PIXEL + dx) * PIXEL, round(centre.y() / PIXEL + dy) * PIXEL, PIXEL, PIXEL),
                        colour,
                    )

    def _paint_sun(self, painter: QPainter, progress: float) -> None:
        radius = 4.6

        def colour_at(dx, dy, distance):
            if distance <= radius:
                return _mix("#FFF6D6", "#FFC252", distance / radius)
            if distance <= radius + 4.5:
                glow = QColor("#FFD27A")
                glow.setAlpha(int(110 * (1 - (distance - radius) / 4.5) ** 1.6))
                return glow
            return None

        self._pixel_disc(painter, self._celestial_centre(progress), radius, colour_at)

    def _paint_moon(self, painter: QPainter, progress: float, cycle: float) -> None:
        radius = 3.8
        offset = math.cos(2 * math.pi * cycle)
        sign = 1 if cycle < 0.5 else -1

        def colour_at(dx, dy, distance):
            if distance <= radius:
                half = math.sqrt(max(0.0, radius * radius - dy * dy)) or 1.0
                lit = sign * dx / half >= offset
                return QColor("#F0E9D5") if lit else QColor(52, 70, 80, 150)
            if distance <= radius + 3:
                glow = QColor("#CBEAF0")
                glow.setAlpha(int(40 * (1 - (distance - radius) / 3)))
                return glow
            return None

        self._pixel_disc(painter, self._celestial_centre(progress), radius, colour_at)

    def _paint_stars(self, painter: QPainter, elapsed: float) -> None:
        opacity = self._state["star_opacity"]
        if opacity <= 0.01:
            return
        for index, (fx, fy) in enumerate(STARS):
            twinkle = 0.7 + 0.3 * math.sin(elapsed * (0.8 + index * 0.17) + index)
            star = QColor("#F4FAFF")
            star.setAlphaF(min(1.0, opacity * twinkle))
            size = 2 if index % 3 else 3
            painter.fillRect(QRectF(round(fx * self.width()), round(fy * self.height()), size, size), star)

    def _cloud_path(self) -> QPainterPath:
        path = QPainterPath()
        for row, line in enumerate(CLOUD):
            for column, cell in enumerate(line):
                if cell == "#":
                    path.addRect(QRectF(column * PIXEL * 1.5, row * PIXEL * 1.5, PIXEL * 1.5, PIXEL * 1.5))
        return path

    def _paint_clouds(self, painter: QPainter, elapsed: float) -> None:
        phase = self._state["phase"]
        body = QColor("#FFFFFF" if phase == "day" else "#E8D6D2" if phase in ("dawn", "dusk") else "#7E8E96")
        body.setAlpha(205 if phase == "day" else 160)
        shade = QColor(body).darker(118)
        span = self.width() + 160
        for top, period, start, scale in ((16, 120.0, 0.1, 1.0), (40, 170.0, 0.6, 0.75)):
            x = -80 + ((elapsed / period + start) % 1.0) * span
            painter.save()
            painter.translate(round(x / PIXEL) * PIXEL, top)
            painter.scale(scale, scale)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.fillPath(self._cloud, body)
            painter.fillRect(QRectF(PIXEL * 1.5, PIXEL * 4.5, PIXEL * 18, PIXEL * 1.5), shade)
            painter.restore()

    def _paint_voice(self, painter: QPainter, elapsed: float) -> None:
        columns = self._geometry[3]
        rows = list(self._voice)
        band = band_range(columns)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.mode == "idle" and not any(any(row) for row in rows):
            # A breathing strip of light along the front of the stage.
            glow = QColor(COLORS["accent"])
            glow.setAlpha(int(70 + 40 * math.sin(elapsed * 1.6)))
            painter.setBrush(glow)
            front = [self._point(band.start, 0.05, 1.02), self._point(band.stop, 0.05, 1.02),
                     self._point(band.stop, 0.25, 1.02), self._point(band.start, 0.25, 1.02)]
            painter.drawPolygon(QPolygonF(front))
            return
        busy = self.mode == "busy"
        hot = QColor(COLORS["cyan"] if busy else COLORS["accent"])
        bright = QColor("#B9F1F8" if busy else COLORS["accent_light"])
        total = 0
        for row in range(len(rows) - 1, -1, -1):
            heights = rows[row]
            if len(heights) != columns:
                continue
            fade = 0.78**row
            for column in self._order(columns):
                height = heights[column]
                if height <= 0:
                    continue
                total += height
                ground = 1
                top = _mix(bright, "#FFFFFF", 0.25)
                top.setAlphaF(fade)
                front = _mix(hot, bright, min(1.0, height / MAX_VOICE_VOXELS))
                front.setAlphaF(fade)
                side = QColor(front).darker(135)
                side.setAlphaF(fade)
                self._cuboid(painter, column + 0.08, column + 0.92, row + 0.08, row + 0.92,
                             ground, ground + height, top, front, side)
                # Seams between stacked voxels, LED-equaliser style.
                seam = QColor(0, 0, 0, int(70 * fade))
                painter.setPen(QPen(seam, 1))
                for level in range(1, height):
                    painter.drawLine(self._point(column + 0.08, row + 0.08, ground + level),
                                     self._point(column + 0.92, row + 0.08, ground + level))
                painter.setPen(Qt.PenStyle.NoPen)
        if total:
            # Warm light spilling over the scene, stronger the louder you speak.
            centre = self._point(columns / 2, 0.5, 3)
            radius = self.width() * 0.45
            light = QRadialGradient(centre, radius)
            colour = QColor(hot)
            colour.setAlpha(min(120, 20 + total))
            light.setColorAt(0, colour)
            colour.setAlpha(0)
            light.setColorAt(1, colour)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            painter.fillRect(QRectF(self.rect()), light)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)


class MicWheel(QAbstractButton):
    """LumaDeck's time wheel as a record button, built from voxel segments.

    Twenty-four blocks ring the face: at rest the top block glows, while
    listening the blocks light clockwise with the recording (once round per
    minute, the length of a held dictation), and while the model works a
    cyan group circles.
    """

    SEGMENTS = 24

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(118, 118)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.state = "idle"
        self.caption = ""
        self._level = 0.0
        self._started = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._time_font = QFont("Segoe UI Variable Text")
        self._time_font.setPixelSize(16)
        self._time_font.setWeight(QFont.Weight.DemiBold)
        self._caption_font = QFont("Segoe UI Variable Text")
        self._caption_font.setPixelSize(10)
        self._caption_font.setWeight(QFont.Weight.DemiBold)
        self._glyph_font = icon_font(20)

    def set_state(self, state: str, caption: str) -> None:
        if state != self.state and state in ("listening", "busy"):
            self._started = time.monotonic()
        self.state = state
        self.caption = caption
        if state in ("listening", "busy"):
            self._timer.start()
        else:
            self._timer.stop()
            self._level = 0.0
        self.update()

    def set_level(self, level: float) -> None:
        self._level = max(self._level, max(0.0, min(1.0, level)))

    def _tick(self) -> None:
        self._level *= 0.85
        self.update()

    def _block(self, painter: QPainter, centre: QPointF, size: float, colour: QColor) -> None:
        """A tiny voxel: top, left and right faces of an isometric cube."""
        half = size / 2
        quarter = size / 4
        top = QPolygonF([centre + QPointF(0, -half), centre + QPointF(half, -quarter),
                         centre + QPointF(0, 0), centre + QPointF(-half, -quarter)])
        left = QPolygonF([centre + QPointF(-half, -quarter), centre, centre + QPointF(0, half),
                          centre + QPointF(-half, quarter)])
        right = QPolygonF([centre, centre + QPointF(half, -quarter), centre + QPointF(half, quarter),
                           centre + QPointF(0, half)])
        for face, tone in ((top, colour.lighter(130)), (left, colour), (right, colour.darker(140))):
            painter.setBrush(tone)
            painter.drawPolygon(face)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = QPointF(self.width() / 2, self.height() / 2)
        elapsed = time.monotonic() - self._started
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#070707"))
        painter.drawEllipse(centre, 59, 59)
        painter.setPen(QPen(QColor("#333333" if self.underMouse() else "#1F1F1F"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, 56.5, 56.5)
        painter.setPen(Qt.PenStyle.NoPen)

        if self.state == "listening":
            lit_until = ((elapsed / 60.0) % 1.0) * self.SEGMENTS
        elif self.state == "busy":
            head = (elapsed * 14) % self.SEGMENTS
        for index in range(self.SEGMENTS):
            angle = math.radians(index * 360 / self.SEGMENTS - 90)
            spot = centre + QPointF(math.cos(angle), math.sin(angle)) * 44
            colour = QColor("#262626")
            if self.state == "listening" and index <= lit_until:
                colour = _mix(COLORS["accent"], COLORS["accent_light"], min(1.0, self._level * 1.8))
            elif self.state == "busy" and 0 <= (head - index) % self.SEGMENTS < 4:
                colour = QColor(COLORS["cyan"])
            elif self.state in ("idle", "error") and index == 0:
                colour = QColor(COLORS["error"] if self.state == "error" else COLORS["accent"])
            if index == 0 or colour.name() != "#262626":
                glow = QRadialGradient(spot, 11)
                halo = QColor(colour)
                halo.setAlpha(90 if colour.name() != "#262626" else 0)
                glow.setColorAt(0, halo)
                halo.setAlpha(0)
                glow.setColorAt(1, halo)
                painter.setBrush(glow)
                painter.drawEllipse(spot, 11, 11)
            self._block(painter, spot, 9.5, colour)

        painter.setPen(QColor(COLORS["text"]))
        top = QRectF(0, centre.y() - 20, self.width(), 22)
        if self.state == "listening":
            painter.setFont(self._time_font)
            painter.drawText(top, Qt.AlignmentFlag.AlignCenter, f"{int(elapsed) // 60}:{int(elapsed) % 60:02d}")
        elif self._glyph_font is not None:
            painter.setFont(self._glyph_font)
            painter.drawText(top, Qt.AlignmentFlag.AlignCenter, "")
        else:
            painter.setFont(self._time_font)
            painter.drawText(top, Qt.AlignmentFlag.AlignCenter, "MIC")
        painter.setFont(self._caption_font)
        tone = {"busy": COLORS["cyan"], "error": COLORS["error"]}.get(self.state, COLORS["accent"])
        painter.setPen(QColor(tone))
        caption = painter.fontMetrics().elidedText(self.caption, Qt.TextElideMode.ElideRight, 72)
        painter.drawText(QRectF(0, centre.y() + 3, self.width(), 16), Qt.AlignmentFlag.AlignCenter, caption)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)
