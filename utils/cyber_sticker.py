"""
cyber_sticker.py  -  Neon season sticker generator
====================================================

Canvas: 400x200 dark-card with neon glow effects

START sticker (gold/amber neon on dark card):
    down  Season 01  down
    star  CyberBot  star

END sticker (green neon + underline on dark card):
    check  Season 03  Done
    8  Files  Sent  (double neon underline)
    star  CyberBot  star

Config (config.py):
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
    """Glowing neon line."""
    g  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(g)
    for w in [10, 6, 3]:
        gd.line([(x1, y1), (x2, y2)], fill=(*glow_color, 50), width=w)
    img = Image.alpha_composite(img, g.filter(ImageFilter.GaussianBlur(4)))
    ImageDraw.Draw(img).line([(x1, y1), (x2, y2)],
                              fill=(*line_color, 255), width=width)
    return img


def _dark_card(img):
    """Rounded dark semi-transparent card behind the neon text."""
    bg   = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(bg)
    m    = 8
    draw.rounded_rectangle([m, m, W-m, H-m],
                            radius=20, fill=(10, 10, 20, 180))
    draw.rounded_rectangle([m, m, W-m, H-m],
                            radius=20, outline=(60, 60, 120, 120), width=1)
    return Image.alpha_composite(bg, img)


def make_start_sticker(season: int, powered_by: str = "Series Bot") -> io.BytesIO:
    ARROW = "\u2b07"
    STAR  = "\u2605"
    line1 = f"{ARROW}  Season {season:02d}  {ARROW}"
    line2 = f"{STAR}  {powered_by}  {STAR}"

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f1  = _fit_font(line1, _BOLD, W - 40, 62)
    f2  = _fit_font(line2, _REG,  W - 60, 26)

    draw = ImageDraw.Draw(img)
    bb1  = draw.textbbox((0, 0), line1, font=f1)
    bb2  = draw.textbbox((0, 0), line2, font=f2)
    tw1, th1 = bb1[2]-bb1[0], bb1[3]-bb1[1]
    tw2, th2 = bb2[2]-bb2[0], bb2[3]-bb2[1]

    y1 = (H - (th1 + 14 + th2)) // 2 - 2
    y2 = y1 + th1 + 14
    x1 = (W - tw1) // 2
    x2 = (W - tw2) // 2

    img = _neon(img, line1, x1, y1, f1, (255, 250, 170), (255, 150, 0),   radius=16)
    img = _neon(img, line2, x2, y2, f2, (180, 240, 255), (0,   180, 255), radius=9)
    img = _dark_card(img)

    buf      = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_start.webp"
    return buf


def make_end_sticker(season: int, total: int,
                     powered_by: str = "Series Bot") -> io.BytesIO:
    CHECK = "\u2714"
    STAR  = "\u2605"
    line1 = f"{CHECK}  Season {season:02d}  Done"
    line2 = f"{total}  Files  Sent"
    line3 = f"{STAR}  {powered_by}  {STAR}"

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f1  = _fit_font(line1, _BOLD, W - 40, 52)
    f2  = _fit_font(line2, _BOLD, W - 40, 46)
    f3  = _fit_font(line3, _REG,  W - 60, 22)

    draw = ImageDraw.Draw(img)
    bb1  = draw.textbbox((0, 0), line1, font=f1)
    bb2  = draw.textbbox((0, 0), line2, font=f2)
    bb3  = draw.textbbox((0, 0), line3, font=f3)
    tw1, th1 = bb1[2]-bb1[0], bb1[3]-bb1[1]
    tw2, th2 = bb2[2]-bb2[0], bb2[3]-bb2[1]
    tw3, th3 = bb3[2]-bb3[0], bb3[3]-bb3[1]

    total_h = th1 + 6 + th2 + 16 + th3
    y1   = (H - total_h) // 2 - 2
    y2   = y1 + th1 + 6
    ul_y = y2 + th2 + 6
    y3   = ul_y + 14
    x1 = (W - tw1) // 2
    x2 = (W - tw2) // 2
    x3 = (W - tw3) // 2

    img = _neon(img, line1, x1, y1, f1, (180, 255, 180), (0, 255, 80),  radius=13)
    img = _neon(img, line2, x2, y2, f2, (255, 255, 255), (0, 255, 120), radius=15)
    pad = 22
    img = _neon_line(img, x2-pad,    ul_y,   x2+tw2+pad,    ul_y,
                     (0, 255, 80),  (120, 255, 140), width=2)
    img = _neon_line(img, x2-pad+12, ul_y+5, x2+tw2+pad-12, ul_y+5,
                     (0, 200, 60),  (80,  220, 100), width=1)
    img = _neon(img, line3, x3, y3, f3, (180, 240, 255), (0, 180, 255), radius=8)
    img = _dark_card(img)

    buf      = io.BytesIO()
    img.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = f"season_{season:02d}_end.webp"
    return buf
