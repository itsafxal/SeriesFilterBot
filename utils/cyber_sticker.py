"""
cyber_sticker.py  —  Clean Neon Season Sticker Generator
=========================================================

START sticker — 2 styles picked randomly per season (saved to DB):
    Style 0 — big bold  ▼ SEASON 02 ▼  white core / red glow
    Style 1 — amber neon ⬇ Season 02 ⬇  + cyan star sub-line

END sticker — 1 style only:
    Style 0 — ◀ THE END ▶  pure blue neon, centred, full-size

Public API:
    make_start_sticker(season, style_idx=None) -> (BytesIO, int)
    make_end_sticker(season, total, style_idx=None) -> (BytesIO, int)

NOTE: PIL (~40MB) is lazy-loaded only when a sticker is actually generated,
not at import time. Font paths are resolved once on first use.
"""

import io
import os
import random

# ─────────────────────────────────────────────────────────────
# LAZY PIL + FONT INIT  (avoids ~40MB RAM at import-time)
# ─────────────────────────────────────────────────────────────

_PIL_LOADED = False
_Image = _ImageDraw = _ImageFont = _ImageFilter = None
_BOLD = _REG = None
W, H = 512, 200

def _ensure_pil():
    """Load PIL and resolve fonts on first call — ~40MB RAM only when needed."""
    global _PIL_LOADED, _Image, _ImageDraw, _ImageFont, _ImageFilter, _BOLD, _REG
    if _PIL_LOADED:
        return
    from PIL import Image as _img, ImageDraw as _drw, ImageFont as _fnt, ImageFilter as _flt
    _Image = _img
    _ImageDraw = _drw
    _ImageFont = _fnt
    _ImageFilter = _flt

    _BOLD_CANDIDATES = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    _REG_CANDIDATES = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    def _find(paths):
        for p in paths:
            if os.path.exists(p):
                return p
        return None
    _BOLD = _find(_BOLD_CANDIDATES)
    _REG = _find(_REG_CANDIDATES) or _BOLD
    _PIL_LOADED = True


def _fit_font(text: str, path: str, max_w: int, start: int):
    _ensure_pil()
    dummy = _ImageDraw.Draw(_Image.new("RGBA", (1, 1)))
    size  = start
    while size > 12:
        f  = _ImageFont.truetype(path, size)
        bb = dummy.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
        size -= 2
    return _ImageFont.truetype(path, 12)


def _sz(draw, text: str, font) -> tuple[int, int]:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _neon(
    img,
    text: str,
    x: int,
    y: int,
    font,
    core_color: tuple,
    glow_color: tuple,
    radius: int = 14,
):
    _ensure_pil()
    for r, alpha_scale in [(radius, 1.0), (max(radius // 2, 4), 0.65), (3, 0.4)]:
        glow = _Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd   = _ImageDraw.Draw(glow)
        for i in range(10, 0, -1):
            gd.text(
                (x, y), text, font=font,
                fill=(*glow_color, int((12 + i * 13) * alpha_scale)),
            )
        img = _Image.alpha_composite(img, glow.filter(_ImageFilter.GaussianBlur(r)))
    _ImageDraw.Draw(img).text((x, y), text, font=font, fill=(*core_color, 255))
    return img


def _blank():
    _ensure_pil()
    return _Image.new("RGBA", (W, H), (0, 0, 0, 0))


def _start_0(season: int):
    _ensure_pil()
    TRI  = "\u25bc"
    text = f"{TRI}  SEASON {season:02d}  {TRI}"
    img  = _blank()
    draw = _ImageDraw.Draw(img)
    f    = _fit_font(text, _BOLD, W - 48, 82)
    tw, th = _sz(draw, text, f)
    img = _neon(
        img, text,
        (W - tw) // 2, (H - th) // 2,
        f,
        core_color=(255, 255, 255),
        glow_color=(210, 20, 0),
        radius=22,
    )
    return img


def _start_1(season: int):
    _ensure_pil()
    ARR  = "\u2b07"
    STAR = "\u2605"
    line1 = f"{ARR}  Season {season:02d}  {ARR}"
    line2 = f"{STAR}  CyberBot  {STAR}"
    img  = _blank()
    draw = _ImageDraw.Draw(img)
    f1 = _fit_font(line1, _BOLD, W - 48, 74)
    f2 = _fit_font(line2, _REG,  W - 80, 28)
    tw1, th1 = _sz(draw, line1, f1)
    tw2, th2 = _sz(draw, line2, f2)
    gap = 16
    total_h = th1 + gap + th2
    y1 = (H - total_h) // 2 - 2
    y2 = y1 + th1 + gap
    img = _neon(
        img, line1,
        (W - tw1) // 2, y1,
        f1,
        core_color=(255, 248, 160),
        glow_color=(255, 140, 0),
        radius=18,
    )
    img = _neon(
        img, line2,
        (W - tw2) // 2, y2,
        f2,
        core_color=(160, 235, 255),
        glow_color=(0, 170, 255),
        radius=10,
    )
    return img


_START_STYLES = [_start_0, _start_1]


def _end_0(season: int, total: int):
    _ensure_pil()
    L    = "\u25c4"
    R    = "\u25ba"
    text = f"{L}  THE END  {R}"
    img  = _blank()
    draw = _ImageDraw.Draw(img)
    f    = _fit_font(text, _BOLD, W - 40, 86)
    tw, th = _sz(draw, text, f)
    img = _neon(
        img, text,
        (W - tw) // 2, (H - th) // 2,
        f,
        core_color=(255, 255, 255),
        glow_color=(0, 140, 255),
        radius=26,
    )
    return img


_END_STYLES = [_end_0]


def make_start_sticker(season: int, style_idx=None) -> tuple[io.BytesIO, int]:
    if style_idx is None:
        style_idx = random.randrange(len(_START_STYLES))
    else:
        style_idx = int(style_idx) % len(_START_STYLES)
    img = _START_STYLES[style_idx](season)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_start.webp"
    return buf, style_idx


def make_end_sticker(season: int, total: int, style_idx=None) -> tuple[io.BytesIO, int]:
    style_idx = 0  # only one end style
    img = _END_STYLES[0](season, total)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_end.webp"
    return buf, style_idx
