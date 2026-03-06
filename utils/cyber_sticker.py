"""
cyber_sticker.py  -  Neon season sticker generator  (multi-style edition)
==========================================================================

Canvas: 400x200 dark-card with neon glow effects

START sticker — randomly picks one of 2 styles (saved to DB per season):
    Style A  (red/orange triangles):
        ▼  SEASON 02  ▼          ← big bold white/red
    Style B  (cyan/blue arrows):
        ↓  Season 02  ↓          ← amber neon + CyberBot sub-line

END sticker — randomly picks one of 3 styles (saved to DB per season):
    Style 1  "THE END" chevrons:
        ◀  THE END  ▶
    Style 2  neon underline bar:
        ✔  Season 03  Done
        8  Files  Sent  (double neon underline)
    Style 3  glowing centre block:
        ━━━━━━━━━━━━━━━━━
        SEASON 03 COMPLETE
        ━━━━━━━━━━━━━━━━━

Config (config.py):
    STICKER_ENABLED    = True
    STICKER_POWERED_BY = "CyberTG ❤"

DB helpers (database.py):
    get_sticker_style(season, kind)   → int | None
    save_sticker_style(season, kind, style_idx)
    get_cached_sticker / save_sticker_cache  (unchanged)
"""

import io
import os
import random

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ─────────────────────────── font paths ────────────────────────────
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

# ─────────────────────────── drawing helpers ───────────────────────

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


def _neon(img, text, x, y, font, color, glow_color, radius=14):
    """Multi-pass neon glow then sharp core text."""
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
    """Glowing neon horizontal/diagonal line."""
    g  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(g)
    for w in [10, 6, 3]:
        gd.line([(x1, y1), (x2, y2)], fill=(*glow_color, 50), width=w)
    img = Image.alpha_composite(img, g.filter(ImageFilter.GaussianBlur(4)))
    ImageDraw.Draw(img).line([(x1, y1), (x2, y2)],
                              fill=(*line_color, 255), width=width)
    return img


def _dark_card(img, tint=(10, 10, 20)):
    """Rounded dark semi-transparent card behind the neon text."""
    bg   = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(bg)
    m    = 8
    draw.rounded_rectangle([m, m, W-m, H-m],
                            radius=20, fill=(*tint, 180))
    draw.rounded_rectangle([m, m, W-m, H-m],
                            radius=20, outline=(60, 60, 120, 120), width=1)
    return Image.alpha_composite(bg, img)


def _cx(img_draw, text, font):
    """Return centred x for text."""
    bb = img_draw.textbbox((0, 0), text, font=font)
    return (W - (bb[2] - bb[0])) // 2


def _text_size(img_draw, text, font):
    bb = img_draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


# ═══════════════════════════════════════════════════════════════════
#  START STICKER  — 2 styles
# ═══════════════════════════════════════════════════════════════════

def _start_style_A(season: int) -> Image.Image:
    """
    Style A — red/orange downward triangles (like screenshot 2 top row)
        ▼  SEASON 02  ▼
        (sub-label)
    """
    TRI   = "\u25bc"           # ▼
    label = f"{TRI}  SEASON {season:02d}  {TRI}"

    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f1   = _fit_font(label, _BOLD, W - 40, 66)
    f2   = _fit_font(sub,   _REG,  W - 80, 22)

    tw1, th1 = _text_size(draw, label, f1)
    tw2, th2 = _text_size(draw, sub,   f2)

    gap  = 14
    y1   = (H - (th1 + gap + th2)) // 2 - 4
    y2   = y1 + th1 + gap
    x1   = (W - tw1) // 2
    x2   = (W - tw2) // 2

    # Main label — white core, red/orange glow
    img = _neon(img, label, x1, y1, f1, (255, 255, 255), (220, 40, 0), radius=18)
    # Sub label — cyan glow
    img = _neon(img, sub,   x2, y2, f2, (180, 240, 255), (0, 180, 255), radius=8)
    return _dark_card(img, tint=(8, 5, 5))


def _start_style_B(season: int) -> Image.Image:
    """
    Style B — cyan/blue downward arrows (screenshot 2 bottom row)
        ↓  Season 02  ↓
        ★  CyberBot  ★
    """
    ARR  = "\u2b07"            # ⬇
    STAR = "\u2605"
    line1 = f"{ARR}  Season {season:02d}  {ARR}"
    line2 = f"{STAR}  CyberBot  {STAR}"

    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f1   = _fit_font(line1, _BOLD, W - 40, 62)
    f2   = _fit_font(line2, _REG,  W - 60, 26)

    tw1, th1 = _text_size(draw, line1, f1)
    tw2, th2 = _text_size(draw, line2, f2)

    gap  = 14
    y1   = (H - (th1 + gap + th2)) // 2 - 2
    y2   = y1 + th1 + gap
    x1   = (W - tw1) // 2
    x2   = (W - tw2) // 2

    # Main — amber neon
    img = _neon(img, line1, x1, y1, f1, (255, 250, 170), (255, 150, 0),  radius=16)
    # Sub  — cyan neon
    img = _neon(img, line2, x2, y2, f2, (180, 240, 255), (0,  180, 255), radius=9)
    return _dark_card(img, tint=(5, 8, 18))


_START_STYLES = [_start_style_A, _start_style_B]


# ═══════════════════════════════════════════════════════════════════
#  END STICKER  — 3 styles
# ═══════════════════════════════════════════════════════════════════

def _end_style_1(season: int, total: int) -> Image.Image:
    """
    Style 1 — "THE END" chevrons (screenshot 1 bottom)
        ◀  THE END  ▶
        Season XX · total files
        ★  CyberBot  ★
    """
    L_ARR = "\u25c4"           # ◀
    R_ARR = "\u25ba"           # ▶
    STAR  = "\u2605"
    line1 = f"{L_ARR}  THE END  {R_ARR}"
    line2 = f"Season {season:02d}  ·  {total} Files"
    line3 = f"{STAR}  CyberBot  {STAR}"

    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f1   = _fit_font(line1, _BOLD, W - 30, 64)
    f2   = _fit_font(line2, _REG,  W - 50, 26)
    f3   = _fit_font(line3, _REG,  W - 60, 20)

    tw1, th1 = _text_size(draw, line1, f1)
    tw2, th2 = _text_size(draw, line2, f2)
    tw3, th3 = _text_size(draw, line3, f3)

    total_h = th1 + 8 + th2 + 10 + th3
    y1 = (H - total_h) // 2 - 4
    y2 = y1 + th1 + 8
    y3 = y2 + th2 + 10
    x1 = (W - tw1) // 2
    x2 = (W - tw2) // 2
    x3 = (W - tw3) // 2

    # "THE END" — white core, bright blue glow
    img = _neon(img, line1, x1, y1, f1, (255, 255, 255), (0, 160, 255), radius=20)
    img = _neon(img, line2, x2, y2, f2, (200, 240, 255), (0, 200, 255), radius=9)
    img = _neon(img, line3, x3, y3, f3, (180, 240, 255), (0, 180, 255), radius=7)
    return _dark_card(img, tint=(5, 8, 20))


def _end_style_2(season: int, total: int) -> Image.Image:
    """
    Style 2 — green check + double neon underline (original design)
        ✔  Season 03  Done
        8  Files  Sent  ══════
        ★  CyberBot  ★
    """
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

    tw1, th1 = _text_size(draw, line1, f1)
    tw2, th2 = _text_size(draw, line2, f2)
    tw3, th3 = _text_size(draw, line3, f3)

    total_h = th1 + 6 + th2 + 16 + th3
    y1   = (H - total_h) // 2 - 2
    y2   = y1 + th1 + 6
    ul_y = y2 + th2 + 6
    y3   = ul_y + 14
    x1   = (W - tw1) // 2
    x2   = (W - tw2) // 2
    x3   = (W - tw3) // 2

    img = _neon(img, line1, x1, y1, f1, (180, 255, 180), (0, 255, 80),  radius=13)
    img = _neon(img, line2, x2, y2, f2, (255, 255, 255), (0, 255, 120), radius=15)
    pad = 22
    img = _neon_line(img, x2-pad,    ul_y,   x2+tw2+pad,    ul_y,
                     (0, 255, 80),  (120, 255, 140), width=2)
    img = _neon_line(img, x2-pad+12, ul_y+5, x2+tw2+pad-12, ul_y+5,
                     (0, 200, 60),  (80,  220, 100), width=1)
    img = _neon(img, line3, x3, y3, f3, (180, 240, 255), (0, 180, 255), radius=8)
    return _dark_card(img, tint=(5, 18, 8))


def _end_style_3(season: int, total: int) -> Image.Image:
    """
    Style 3 — glowing horizontal bars + centre block
        ━━━━━━━━━━━━━━━━━━━━━
        SEASON 03  COMPLETE
        total files  ·  CyberBot
        ━━━━━━━━━━━━━━━━━━━━━
    """
    STAR  = "\u2605"
    line1 = f"SEASON {season:02d}  COMPLETE"
    line2 = f"{total} Files  ·  CyberBot"

    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f1   = _fit_font(line1, _BOLD, W - 40, 58)
    f2   = _fit_font(line2, _REG,  W - 60, 22)

    tw1, th1 = _text_size(draw, line1, f1)
    tw2, th2 = _text_size(draw, line2, f2)

    bar_gap = 12
    total_h  = 4 + bar_gap + th1 + 8 + th2 + bar_gap + 4
    y_bar1   = (H - total_h) // 2
    y1       = y_bar1 + bar_gap
    y2       = y1 + th1 + 8
    y_bar2   = y2 + th2 + bar_gap
    x1       = (W - tw1) // 2
    x2       = (W - tw2) // 2
    pad      = 30

    # Top bar
    img = _neon_line(img, pad, y_bar1, W-pad, y_bar1,
                     (180, 0, 255), (220, 100, 255), width=3)
    # Main text — purple/magenta neon
    img = _neon(img, line1, x1, y1, f1, (255, 220, 255), (200, 0, 255), radius=18)
    # Sub text — soft cyan
    img = _neon(img, line2, x2, y2, f2, (200, 230, 255), (0, 180, 255),  radius=8)
    # Bottom bar
    img = _neon_line(img, pad, y_bar2, W-pad, y_bar2,
                     (180, 0, 255), (220, 100, 255), width=3)
    return _dark_card(img, tint=(12, 5, 20))


_END_STYLES = [_end_style_1, _end_style_2, _end_style_3]


# ═══════════════════════════════════════════════════════════════════
#  PUBLIC API  —  called from files.py / bot handlers
# ═══════════════════════════════════════════════════════════════════

def make_start_sticker(
    season: int,
    style_idx: int | None = None,
) -> tuple[io.BytesIO, int]:
    """
    Returns (BytesIO webp, chosen_style_idx).
    Pass style_idx to reuse a previously saved style; None = random.
    """
    if style_idx is None or not (0 <= style_idx < len(_START_STYLES)):
        style_idx = random.randrange(len(_START_STYLES))
    img = _START_STYLES[style_idx](season)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_start.webp"
    return buf, style_idx


def make_end_sticker(
    season: int,
    total: int,
    style_idx: int | None = None,
) -> tuple[io.BytesIO, int]:
    """
    Returns (BytesIO webp, chosen_style_idx).
    Pass style_idx to reuse a previously saved style; None = random.
    """
    if style_idx is None or not (0 <= style_idx < len(_END_STYLES)):
        style_idx = random.randrange(len(_END_STYLES))
    img = _END_STYLES[style_idx](season, total)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_end.webp"
    return buf, style_idx
