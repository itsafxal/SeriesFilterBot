"""
cyber_sticker.py  —  Season sticker generator
===============================================

Generates a transparent-background WebP sticker for each season delivery.

Layout:
  ┌─────────────────────────────┐
  │                             │
  │        Season 03            │  ← big bold white text  (88px)
  │    powered by CyberBot      │  ← small blue-tinted text (30px)
  │                             │
  └─────────────────────────────┘

Config keys (in config.py):
  STICKER_POWERED_BY   = "CyberBot"      ← change to your brand name
  STICKER_ENABLED      = True            ← set False to disable sticker send
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ── Font paths (DejaVu ships with most Linux distros) ─────────────────
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
_FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]

def _find_font(candidates: list) -> str | None:
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

_FONT_BOLD_PATH = _find_font(_FONT_CANDIDATES_BOLD)
_FONT_REG_PATH  = _find_font(_FONT_CANDIDATES_REG) or _FONT_BOLD_PATH

# Sticker canvas size (Telegram sticker: 512×512)
_W = 512
_H = 512


def make_season_sticker(season: int, powered_by: str = "Series Bot") -> io.BytesIO:
    """
    Generate a 512×512 transparent WebP sticker.

    Args:
        season    : season number (e.g. 3 → "Season 03")
        powered_by: brand name shown below (e.g. "CyberBot")

    Returns:
        BytesIO containing the WebP image, seeked to position 0.
    """
    season_text  = f"Season {season:02d}"
    powered_text = f"powered by {powered_by}"

    img  = Image.new("RGBA", (_W, _H), (0, 0, 0, 0))

    # ── Load fonts ────────────────────────────────────────────────────
    try:
        f_big = (
            ImageFont.truetype(_FONT_BOLD_PATH, 88)
            if _FONT_BOLD_PATH
            else ImageFont.load_default()
        )
        f_small = (
            ImageFont.truetype(_FONT_REG_PATH, 30)
            if _FONT_REG_PATH
            else ImageFont.load_default()
        )
    except Exception:
        f_big   = ImageFont.load_default()
        f_small = ImageFont.load_default()

    draw = ImageDraw.Draw(img)

    # ── Measure season text ───────────────────────────────────────────
    bb      = draw.textbbox((0, 0), season_text, font=f_big)
    tw, th  = bb[2] - bb[0], bb[3] - bb[1]
    x       = (_W - tw) // 2
    y       = _H // 2 - th // 2 - 20

    # ── Glow layer (light blue haze behind text) ──────────────────────
    glow = Image.new("RGBA", (_W, _H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    for offset in range(6, 0, -1):
        alpha = 35 + offset * 8
        gd.text((x, y), season_text, font=f_big, fill=(100, 180, 255, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=9))
    img  = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)

    # ── Drop shadow ───────────────────────────────────────────────────
    draw.text((x + 4, y + 4), season_text, font=f_big, fill=(0, 0, 0, 130))

    # ── Main season text (white) ──────────────────────────────────────
    draw.text((x, y), season_text, font=f_big, fill=(255, 255, 255, 255))

    # ── "powered by …" subtitle ───────────────────────────────────────
    bb2  = draw.textbbox((0, 0), powered_text, font=f_small)
    tw2  = bb2[2] - bb2[0]
    x2   = (_W - tw2) // 2
    y2   = y + th + 16
    # subtle shadow
    draw.text((x2 + 2, y2 + 2), powered_text, font=f_small, fill=(0, 0, 0, 110))
    # blue-tinted subtitle
    draw.text((x2, y2), powered_text, font=f_small, fill=(180, 220, 255, 215))

    # ── Encode to WebP (lossless, transparent) ────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    return buf
