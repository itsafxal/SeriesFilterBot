"""
cyber_sticker.py  —  Neon season sticker generator (multi-style edition)
=========================================================================

START sticker — 2 styles picked randomly per season (saved to DB):
    Style 0 — big bold  ▼ SEASON 02 ▼  white core / red glow
    Style 1 — amber neon ⬇ Season 02 ⬇  + cyan star sub-line

END sticker — 3 styles picked randomly per season (saved to DB):
    Style 0 — ◀ THE END ▶  blue neon
    Style 1 — ✔ Season Done  +  N Files Sent  (double green underline)
    Style 2 — purple bar  SEASON COMPLETE  purple bar

Public API:
    make_start_sticker(season, style_idx=None) -> (BytesIO, int)
    make_end_sticker(season, total, style_idx=None) -> (BytesIO, int)
"""

import io
import os
import random

from PIL import Image, ImageDraw, ImageFont, ImageFilter

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
_REG  = _find(_REG_CANDIDATES) or _BOLD
W, H  = 400, 200


def _fit_font(text, path, max_w, start):
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    size  = start
    while size > 12:
        f  = ImageFont.truetype(path, size)
        bb = dummy.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
        size -= 2
    return ImageFont.truetype(path, 12)


def _sz(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _neon(img, text, x, y, font, color, glow_color, radius=14):
    for r, strength in [(radius, 1.0), (radius // 2, 0.7), (3, 0.5)]:
        g  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(g)
        for i in range(8, 0, -1):
            gd.text((x, y), text, font=font,
                    fill=(*glow_color, int((15 + i * 14) * strength)))
        img = Image.alpha_composite(img, g.filter(ImageFilter.GaussianBlur(r)))
    ImageDraw.Draw(img).text((x, y), text, font=font, fill=(*color, 255))
    return img


def _neon_line(img, x1, y1, x2, y2, glow_color, line_color, width=2):
    g  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(g)
    for w in [10, 6, 3]:
        gd.line([(x1, y1), (x2, y2)], fill=(*glow_color, 50), width=w)
    img = Image.alpha_composite(img, g.filter(ImageFilter.GaussianBlur(4)))
    ImageDraw.Draw(img).line([(x1, y1), (x2, y2)],
                              fill=(*line_color, 255), width=width)
    return img


def _dark_card(img, tint=(10, 10, 20)):
    # Transparent — neon text floats over Telegram wallpaper
    return img


# ── START styles ──────────────────────────────────────────────────────

def _start_0(season):
    TRI  = "\u25bc"
    text = f"{TRI}  SEASON {season:02d}  {TRI}"
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f    = _fit_font(text, _BOLD, W - 40, 72)
    tw, th = _sz(draw, text, f)
    img = _neon(img, text, (W - tw) // 2, (H - th) // 2, f,
                (255, 255, 255), (220, 40, 0), radius=20)
    return _dark_card(img, tint=(10, 4, 4))


def _start_1(season):
    ARR  = "\u2b07"
    STAR = "\u2605"
    line1 = f"{ARR}  Season {season:02d}  {ARR}"
    line2 = f"{STAR}  CyberBot  {STAR}"
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f1   = _fit_font(line1, _BOLD, W - 40, 62)
    f2   = _fit_font(line2, _REG,  W - 60, 26)
    tw1, th1 = _sz(draw, line1, f1)
    tw2, th2 = _sz(draw, line2, f2)
    gap = 14
    y1  = (H - (th1 + gap + th2)) // 2 - 2
    y2  = y1 + th1 + gap
    img = _neon(img, line1, (W - tw1) // 2, y1, f1, (255, 250, 170), (255, 150, 0),  radius=16)
    img = _neon(img, line2, (W - tw2) // 2, y2, f2, (180, 240, 255), (0,   180, 255), radius=9)
    return _dark_card(img, tint=(5, 8, 18))


_START_STYLES = [_start_0, _start_1]


# ── END styles ────────────────────────────────────────────────────────

def _end_0(season, total):
    L    = "\u25c4"
    R    = "\u25ba"
    line1 = f"{L}  THE END  {R}"
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f1   = _fit_font(line1, _BOLD, W - 30, 72)
    tw1, th1 = _sz(draw, line1, f1)
    y1 = (H - th1) // 2
    img = _neon(img, line1, (W - tw1) // 2, y1, f1, (255, 255, 255), (0, 160, 255), radius=20)
    return _dark_card(img, tint=(5, 8, 20))


def _end_1(season, total):
    CHECK = "\u2714"
    STAR  = "\u2605"
    line1 = f"{CHECK}  Season {season:02d}  Done"
    line2 = f"{total}  Files  Sent"
    line3 = f"{STAR}  CyberBot  {STAR}"
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f1   = _fit_font(line1, _BOLD, W - 40, 52)
    f2   = _fit_font(line2, _BOLD, W - 40, 46)
    f3   = _fit_font(line3, _REG,  W - 60, 22)
    tw1, th1 = _sz(draw, line1, f1)
    tw2, th2 = _sz(draw, line2, f2)
    tw3, th3 = _sz(draw, line3, f3)
    y1   = (H - (th1 + 6 + th2 + 16 + th3)) // 2 - 2
    y2   = y1 + th1 + 6
    ul_y = y2 + th2 + 6
    y3   = ul_y + 14
    x2   = (W - tw2) // 2
    pad  = 22
    img = _neon(img, line1, (W - tw1) // 2, y1, f1, (180, 255, 180), (0, 255, 80),  radius=13)
    img = _neon(img, line2, x2,             y2, f2, (255, 255, 255), (0, 255, 120), radius=15)
    img = _neon_line(img, x2 - pad,      ul_y,     x2 + tw2 + pad,      ul_y,
                     (0, 255, 80),  (120, 255, 140), width=2)
    img = _neon_line(img, x2 - pad + 12, ul_y + 5, x2 + tw2 + pad - 12, ul_y + 5,
                     (0, 200, 60),  (80,  220, 100), width=1)
    img = _neon(img, line3, (W - tw3) // 2, y3, f3, (180, 240, 255), (0, 180, 255), radius=8)
    return _dark_card(img, tint=(5, 18, 8))


def _end_2(season, total):
    line1 = f"SEASON {season:02d}  COMPLETE"
    line2 = f"{total} Files  \u00b7  CyberBot"
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f1   = _fit_font(line1, _BOLD, W - 40, 58)
    f2   = _fit_font(line2, _REG,  W - 60, 22)
    tw1, th1 = _sz(draw, line1, f1)
    tw2, th2 = _sz(draw, line2, f2)
    bar_gap = 14
    y_bar1  = (H - (bar_gap + th1 + 8 + th2 + bar_gap)) // 2
    y1      = y_bar1 + bar_gap
    y2      = y1 + th1 + 8
    y_bar2  = y2 + th2 + bar_gap
    pad     = 30
    img = _neon_line(img, pad, y_bar1, W - pad, y_bar1,
                     (180, 0, 255), (220, 100, 255), width=3)
    img = _neon(img, line1, (W - tw1) // 2, y1, f1, (255, 220, 255), (200, 0, 255), radius=18)
    img = _neon(img, line2, (W - tw2) // 2, y2, f2, (200, 230, 255), (0,   180, 255), radius=8)
    img = _neon_line(img, pad, y_bar2, W - pad, y_bar2,
                     (180, 0, 255), (220, 100, 255), width=3)
    return _dark_card(img, tint=(12, 5, 20))


_END_STYLES = [_end_0, _end_1, _end_2]


# ── public API ────────────────────────────────────────────────────────

def make_start_sticker(season: int, style_idx=None):
    """
    Returns (BytesIO webp, chosen_style_idx).
    Pass style_idx to reuse a saved style; None = pick randomly.
    """
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


def make_end_sticker(season: int, total: int, style_idx=None):
    """
    Returns (BytesIO webp, chosen_style_idx).
    Pass style_idx to reuse a saved style; None = pick randomly.
    """
    if style_idx is None:
        style_idx = random.randrange(len(_END_STYLES))
    else:
        style_idx = int(style_idx) % len(_END_STYLES)
    img = _END_STYLES[style_idx](season, total)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_end.webp"
    return buf, style_idx
