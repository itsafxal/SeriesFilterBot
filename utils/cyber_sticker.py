"""
cyber_sticker.py  -  Neon season sticker generator
====================================================

Canvas: 384x192 (compact banner)

START sticker  (gold/amber neon):
    down  Season 01  down
    star  CyberBot  star

END sticker  (green neon + glowing underline):
    check  Season 03  Complete
    8  Files  Delivered      <- double neon underline
    star  CyberBot  star

Config keys (config.py):
    STICKER_ENABLED    = True
    STICKER_POWERED_BY = "Series Bot"
"""

import io
import os

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
W, H  = 384, 192


def _fit_font(text, path, max_w, start):
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    size  = start
    while size > 14:
        f  = ImageFont.truetype(path, size)
        bb = dummy.textbbox((0, 0), text, font=f)
        if bb[2] - bb[0] <= max_w:
            return f
        size -= 2
    return ImageFont.truetype(path, 14)


def _neon(img, text, x, y, font, color, glow_color, radius=13):
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    for r in range(9, 0, -1):
        gd.text((x, y), text, font=font, fill=(*glow_color, 18 + r * 13))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(radius)))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(radius // 2)))
    ImageDraw.Draw(img).text((x, y), text, font=font, fill=(*color, 255))
    return img


def _neon_line(img, x1, y1, x2, y2, color, glow_color, width=2):
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    for w in [8, 5, 3]:
        gd.line([(x1, y1), (x2, y2)], fill=(*glow_color, 60), width=w)
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(3)))
    ImageDraw.Draw(img).line([(x1, y1), (x2, y2)], fill=(*color, 240), width=width)
    return img


def make_start_sticker(season: int, powered_by: str = "Series Bot") -> io.BytesIO:
    ARROW = "⬇"
    STAR  = "★"
    line1 = f"{ARROW}  Season {season:02d}  {ARROW}"
    line2 = f"{STAR}  {powered_by}  {STAR}"
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f1  = _fit_font(line1, _BOLD, W - 16, 60)
    f2  = _fit_font(line2, _REG,  W - 30, 25)
    draw = ImageDraw.Draw(img)
    bb1  = draw.textbbox((0, 0), line1, font=f1)
    bb2  = draw.textbbox((0, 0), line2, font=f2)
    tw1, th1 = bb1[2]-bb1[0], bb1[3]-bb1[1]
    tw2, th2 = bb2[2]-bb2[0], bb2[3]-bb2[1]
    y1 = (H - (th1 + 12 + th2)) // 2 - 4
    y2 = y1 + th1 + 12
    x1 = (W - tw1) // 2
    x2 = (W - tw2) // 2
    img = _neon(img, line1, x1, y1, f1, (255, 248, 160), (255, 160, 0),  radius=15)
    img = _neon(img, line2, x2, y2, f2, (210, 255, 255), (0,   200, 255), radius=8)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_start.webp"
    return buf


def make_end_sticker(season: int, total: int, powered_by: str = "Series Bot") -> io.BytesIO:
    CHECK = "✔"
    STAR  = "★"
    line1 = f"{CHECK}  Season {season:02d}  Complete"
    line2 = f"{total}  Files  Delivered"
    line3 = f"{STAR}  {powered_by}  {STAR}"
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f1  = _fit_font(line1, _BOLD, W - 16, 50)
    f2  = _fit_font(line2, _BOLD, W - 30, 44)
    f3  = _fit_font(line3, _REG,  W - 40, 22)
    draw = ImageDraw.Draw(img)
    bb1  = draw.textbbox((0, 0), line1, font=f1)
    bb2  = draw.textbbox((0, 0), line2, font=f2)
    bb3  = draw.textbbox((0, 0), line3, font=f3)
    tw1, th1 = bb1[2]-bb1[0], bb1[3]-bb1[1]
    tw2, th2 = bb2[2]-bb2[0], bb2[3]-bb2[1]
    tw3, th3 = bb3[2]-bb3[0], bb3[3]-bb3[1]
    total_h  = th1 + 8 + th2 + 14 + th3
    y1   = (H - total_h) // 2 - 4
    y2   = y1 + th1 + 8
    ul_y = y2 + th2 + 5
    y3   = ul_y + 12
    x1 = (W - tw1) // 2
    x2 = (W - tw2) // 2
    x3 = (W - tw3) // 2
    img = _neon(img, line1, x1, y1, f1, (210, 255, 210), (0, 255, 80),  radius=13)
    img = _neon(img, line2, x2, y2, f2, (255, 255, 255), (0, 255, 100), radius=14)
    pad = 20
    img = _neon_line(img, x2-pad,    ul_y,   x2+tw2+pad,    ul_y,   (150,255,150), (0,255,80),  width=2)
    img = _neon_line(img, x2-pad+10, ul_y+5, x2+tw2+pad-10, ul_y+5, (100,255,120), (0,200,60),  width=1)
    img = _neon(img, line3, x3, y3, f3, (200, 255, 255), (0, 200, 255), radius=7)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_end.webp"
    return buf
