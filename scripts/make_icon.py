"""Render the app icon into assets/icon.png (256 px) and assets/icon.ico (all sizes).

Large master (40 px and up): voxel mic capsule in a grey cradle, four level bars,
warm floor glow on an OLED tile. Small master (20-32 px): the same without the
outer bars. 16 px: hand-placed pixel art. Run: python scripts/make_icon.py
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "assets"

DEFS = (
    '<defs><linearGradient id="t" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#141414"/>'
    '<stop offset="1" stop-color="#060606"/></linearGradient>'
    '<radialGradient id="h" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#EA8A2B" stop-opacity="0.14"/>'
    '<stop offset="1" stop-color="#EA8A2B" stop-opacity="0"/></radialGradient>'
    '<radialGradient id="f" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#EA8A2B" stop-opacity="0.45"/>'
    '<stop offset="1" stop-color="#EA8A2B" stop-opacity="0"/></radialGradient></defs>'
)
OUTER_BARS = (
    '<polygon points="42,122 47.2,110 47.2,138 42,150" fill="#693E15"/>'
    '<polygon points="30,122 42,122 47.2,110 35.9,110" fill="#856141"/>'
    '<rect x="30" y="122" width="12" height="28" fill="#7A4A1B"/><rect x="30" y="135" width="12" height="2" fill="#59320F"/>'
    '<polygon points="214,122 208.8,110 208.8,138 214,150" fill="#693E15"/>'
    '<polygon points="214,122 226,122 220.1,110 208.8,110" fill="#856141"/>'
    '<rect x="214" y="122" width="12" height="28" fill="#7A4A1B"/><rect x="214" y="135" width="12" height="2" fill="#59320F"/>'
)
BODY = (
    '<polygon points="64,94 67.8,82 67.8,138 64,150" fill="#99571A"/>'
    '<polygon points="52,94 64,94 67.8,82 56.6,82" fill="#C28D5C"/>'
    '<rect x="52" y="94" width="12" height="56" fill="#B26A23"/>'
    '<path d="M52 107h12v2H52zM52 121h12v2H52zM52 135h12v2H52z" fill="#814612"/>'
    '<polygon points="192,94 188.2,82 188.2,138 192,150" fill="#99571A"/>'
    '<polygon points="192,94 204,94 199.4,82 188.2,82" fill="#C28D5C"/>'
    '<rect x="192" y="94" width="12" height="56" fill="#B26A23"/>'
    '<path d="M192 107h12v2h-12zM192 121h12v2h-12zM192 135h12v2h-12z" fill="#814612"/>'
    '<polygon points="76,158 180,158 176.9,146 79.1,146" fill="#5E5E5E"/>'
    '<polygon points="86,104 88.5,92 88.5,146 86,158" fill="#6E6E6E"/>'
    '<polygon points="76,104 86,104 88.5,92 79.1,92" fill="#D9D9D9"/>'
    '<rect x="76" y="104" width="10" height="54" fill="#A3A3A3"/>'
    '<polygon points="170,104 167.5,92 167.5,146 170,158" fill="#6E6E6E"/>'
    '<polygon points="170,104 180,104 176.9,92 167.5,92" fill="#D9D9D9"/>'
    '<rect x="170" y="104" width="10" height="54" fill="#A3A3A3"/>'
    '<rect x="76" y="158" width="104" height="10" fill="#A3A3A3"/>'
    '<polygon points="100,54 156,54 154.3,42 101.7,42" fill="#FFB877"/>'
    '<rect x="100" y="54" width="56" height="90" fill="#EA8A2B"/>'
    '<path d="M100 83h56v3h-56zM100 113h56v3h-56z" fill="#A85A14"/>'
    '<polygon points="94,186 162,186 159.9,174 96,174" fill="#C4C4C4"/>'
    '<rect x="122" y="168" width="12" height="12" fill="#A3A3A3"/>'
    '<rect x="94" y="186" width="68" height="10" fill="#8C8C8C"/>'
)


def master(small: bool) -> str:
    rim = (
        '<rect x="12" y="12" width="232" height="232" rx="52" fill="none" stroke="#2C2C2C" stroke-width="8"/>'
        if small
        else '<rect x="9.5" y="9.5" width="237" height="237" rx="54.5" fill="none" stroke="#2C2C2C" stroke-width="3"/>'
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">' + DEFS
        + '<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#t)"/>' + rim
        + '<ellipse cx="128" cy="104" rx="100" ry="86" fill="url(#h)"/>'
        + '<ellipse cx="128" cy="198" rx="104" ry="24" fill="url(#f)"/>'
        + ("" if small else OUTER_BARS) + BODY + "</svg>"
    )


# 16 px pixel art: E rim, T tile, H capsule top, O capsule, S seam, L lit tops, G cradle, W floor glow.
PIXELS = [
    "..EEEEEEEEEEEE..",
    ".ETTTTTTTTTTTTE.",
    "ETTTTTHHHHTTTTTE",
    "ETTTTOOOOOOTTTTE",
    "ETTTTOOOOOOTTTTE",
    "ETTTTSSSSSSTTTTE",
    "ETTLTOOOOOOTLTTE",
    "ETTGTOOOOOOTGTTE",
    "ETTGTOOOOOOTGTTE",
    "ETTGTTTTTTTTGTTE",
    "ETTGGGGGGGGGGTTE",
    "ETTTTTTGGTTTTTTE",
    "ETTTTTLLLLTTTTTE",
    "ETTTTGGGGGGTTTTE",
    ".ETTWWWWWWWWTTE.",
    "..EEEEEEEEEEEE..",
]
PIXEL_COLORS = {"E": "#2C2C2C", "T": "#0A0A0A", "H": "#FFB877", "O": "#EA8A2B", "S": "#C8711F",
                "L": "#D9D9D9", "G": "#A3A3A3", "W": "#643D17"}


def render(svg: str, size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    return image


def pixel_art() -> QImage:
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    for y, row in enumerate(PIXELS):
        for x, cell in enumerate(row):
            if cell != ".":
                image.setPixelColor(x, y, QColor(PIXEL_COLORS[cell]))
    return image


def png(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def write_ico(path: Path, frames: dict[int, bytes]) -> None:
    """ICO with PNG frames (Windows Vista and later read these at every size)."""
    entries = sorted(frames.items())
    offset = 6 + 16 * len(entries)
    header = struct.pack("<HHH", 0, 1, len(entries))
    directory, data = b"", b""
    for size, blob in entries:
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset + len(data))
        data += blob
    path.write_bytes(header + directory + data)


if __name__ == "__main__":
    app = QGuiApplication([])
    large, small = master(small=False), master(small=True)
    frames = {16: png(pixel_art())}
    for size in (20, 24, 32):
        frames[size] = png(render(small, size))
    for size in (40, 48, 64, 256):
        frames[size] = png(render(large, size))
    write_ico(ASSETS / "icon.ico", frames)
    render(large, 256).save(str(ASSETS / "icon.png"))
    print("wrote", ASSETS / "icon.ico", "and", ASSETS / "icon.png")
