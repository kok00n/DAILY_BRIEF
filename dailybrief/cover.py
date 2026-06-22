"""Generate podcast cover art (required by Spotify/Apple: square, >=1400px).

A clean typographic default is generated with Pillow so the feed works out of
the box; set publish.cover_image_url in config to use your own artwork instead.
"""
from __future__ import annotations

import io

SIZE = 1500
BG = (13, 27, 42)        # deep navy
ACCENT = (34, 197, 142)  # teal
FG = (237, 242, 247)     # near-white
MUTED = (148, 163, 184)  # slate

_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
_FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial.ttf",
]


def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    for path in (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG):
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def generate_cover(title: str, subtitle: str = "") -> bytes:
    """Return PNG bytes of a 1500x1500 cover."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)

    title_font = _font(168, bold=True)
    sub_font = _font(82, bold=False)
    margin = 150
    max_w = SIZE - 2 * margin

    lines = _wrap(d, title.upper(), title_font, max_w)
    line_h = title_font.size + 28
    block_h = line_h * len(lines)
    y = (SIZE - block_h) // 2 - 60
    for ln in lines:
        w = d.textlength(ln, font=title_font)
        d.text(((SIZE - w) / 2, y), ln, font=title_font, fill=FG)
        y += line_h

    # accent underline
    d.rectangle([margin, y + 20, margin + 360, y + 32], fill=ACCENT)

    if subtitle:
        sw = d.textlength(subtitle, font=sub_font)
        d.text(((SIZE - sw) / 2, y + 70), subtitle, font=sub_font, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
