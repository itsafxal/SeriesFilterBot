"""
cyber_sticker.py  —  Season sticker generator
===============================================

Two sticker types:
  START  — "⬇ Season 01 ⬇ / powered by ..."   gold glow, sent before files
  END    — "✔ Season 01 Complete / 8 files delivered (underlined) / powered by ..."
             green glow, sent after delivery, replaces the progress message

Config keys (config.py):
  STICKER_ENABLED    = True
  STICKER_POWERED_BY = "Series Bot"
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont, ImageFilter

_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
_FONT_REG_CANDIDATES = [
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

_BOLD = _find(_FONT_BOLD_CANDIDATES)
_REG  = _find(_FONT_REG_CANDIDATES) or _BOLD
W = H = 512


def _fit_font(text: str, path: str, max_w: int, start: int) -> ImageFont.FreeTypeFont:
    """Shrink font until text fits within max_w pixels."""
    size = start
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while size > 18:
        f  = ImageFont.truetype(path, size)
        bb = dummy.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
        size -= 3
    return ImageFont.truetype(path, 18)


def _glow(img: Image.Image, text: str, x: int, y: int,
          font, rgb: tuple, radius: int = 10) -> Image.Image:
    """Alpha-composite a blurred glow behind text."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd    = ImageDraw.Draw(layer)
    for r in range(7, 0, -1):
        gd.text((x, y), text, font=font, fill=(*rgb, 28 + r * 9))
    layer = layer.filter(ImageFilter.GaussianBlur(radius=radius))
    return Image.alpha_composite(img, layer)


def make_start_sticker(season: int, powered_by: str = "Series Bot") -> io.BytesIO:
    """
    Gold-glow start sticker:  ⬇  Season 01  ⬇
                              powered by Series Bot
    """
    ARROW = "\u2b07"
    line1 = f"{ARROW}  Season {season:02d}  {ARROW}"
    line2 = f"powered by {powered_by}"

    img    = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f_big  = _fit_font(line1, _BOLD, W - 40, 82)
    f_sm   = ImageFont.truetype(_REG, 30)

    draw   = ImageDraw.Draw(img)
    bb1    = draw.textbbox((0, 0), line1, font=f_big)
    bb2    = draw.textbbox((0, 0), line2, font=f_sm)
    tw1, th1 = bb1[2]-bb1[0], bb1[3]-bb1[1]
    tw2, th2 = bb2[2]-bb2[0], bb2[3]-bb2[1]

    total_h = th1 + 16 + th2
    y1 = (H - total_h) // 2 - 10
    y2 = y1 + th1 + 16
    x1 = (W - tw1) // 2
    x2 = (W - tw2) // 2

    # Gold glow
    img  = _glow(img, line1, x1, y1, f_big, (255, 200, 50))
    draw = ImageDraw.Draw(img)

    # Shadow + highlight + white
    draw.text((x1+4, y1+4), line1, font=f_big, fill=(0,   0,   0,   140))
    draw.text((x1+1, y1+1), line1, font=f_big, fill=(255, 220, 80,  170))
    draw.text((x1,   y1),   line1, font=f_big, fill=(255, 255, 255, 255))

    # "powered by" subtitle in gold
    draw.text((x2+2, y2+2), line2, font=f_sm, fill=(0,   0,   0,   120))
    draw.text((x2,   y2),   line2, font=f_sm, fill=(255, 220, 100, 220))

    buf      = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_start.webp"
    return buf


def make_end_sticker(season: int, total: int, powered_by: str = "Series Bot") -> io.BytesIO:
    """
    Green-glow end sticker:  ✔ Season 03 Complete
                             8 files delivered  (with double underline)
                             powered by Series Bot
    """
    CHECK = "\u2714"
    line1 = f"{CHECK} Season {season:02d} Complete"
    line2 = f"{total} files delivered"
    line3 = f"powered by {powered_by}"

    img    = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f_big  = _fit_font(line1, _BOLD,  W - 30, 68)
    f_med  = _fit_font(line2, _REG,   W - 60, 42)
    f_sm   = ImageFont.truetype(_REG, 26)

    draw   = ImageDraw.Draw(img)
    bb1    = draw.textbbox((0, 0), line1, font=f_big)
    bb2    = draw.textbbox((0, 0), line2, font=f_med)
    bb3    = draw.textbbox((0, 0), line3, font=f_sm)
    tw1, th1 = bb1[2]-bb1[0], bb1[3]-bb1[1]
    tw2, th2 = bb2[2]-bb2[0], bb2[3]-bb2[1]
    tw3      = bb3[2]-bb3[0]

    GAP     = 10
    UL_GAP  = 7
    total_h = th1 + GAP + th2 + UL_GAP + 3 + UL_GAP + GAP + (bb3[3]-bb3[1])
    y1  = (H - total_h) // 2 - 6
    y2  = y1 + th1 + GAP
    ul1 = y2 + th2 + UL_GAP
    ul2 = ul1 + 5
    y3  = ul2 + UL_GAP + GAP
    x1  = (W - tw1) // 2
    x2  = (W - tw2) // 2
    x3  = (W - tw3) // 2

    # Green glow on headline
    img  = _glow(img, line1, x1, y1, f_big, (80, 255, 120))
    draw = ImageDraw.Draw(img)

    # Headline shadow + highlight + white
    draw.text((x1+4, y1+4), line1, font=f_big, fill=(0,   0,   0,   130))
    draw.text((x1+1, y1+1), line1, font=f_big, fill=(100, 255, 140, 160))
    draw.text((x1,   y1),   line1, font=f_big, fill=(255, 255, 255, 255))

    # File count in green
    draw.text((x2+2, y2+2), line2, font=f_med, fill=(0,   0,   0,   120))
    draw.text((x2,   y2),   line2, font=f_med, fill=(150, 255, 180, 235))

    # Double underline
    pad = 28
    draw.line([(x2-pad, ul1), (x2+tw2+pad, ul1)], fill=(100, 255, 140, 210), width=3)
    draw.line([(x2-pad+10, ul2), (x2+tw2+pad-10, ul2)], fill=(100, 255, 140, 110), width=1)

    # "powered by" in soft green
    draw.text((x3+2, y3+2), line3, font=f_sm, fill=(0,   0,   0,   110))
    draw.text((x3,   y3),   line3, font=f_sm, fill=(150, 255, 180, 200))

    buf      = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_end.webp"
    return buf
