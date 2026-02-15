from pyrogram.types import InlineKeyboardButton


# -------------------------------------------------
# SMALL CAPS MAP
# -------------------------------------------------

SMALL_CAPS_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ",
    "e": "ᴇ", "f": "ғ", "g": "ɢ", "h": "ʜ",
    "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ",
    "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ",
    "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ",
    "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ"
}


# -------------------------------------------------
# SMALL CAPS TEXT
# -------------------------------------------------

def small_caps(text: str):
    if not text:
        return ""

    return "".join(
        SMALL_CAPS_MAP.get(ch.lower(), ch)
        for ch in str(text)
    )


# -------------------------------------------------
# AUTO SMALL CAPS TEXT (UI)
# -------------------------------------------------

def UI(text: str):
    """
    Auto UI text styling.
    """
    return small_caps(text)


# -------------------------------------------------
# AUTO SMALL CAPS BUTTON
# -------------------------------------------------

def BTN(text, **kwargs):
    """
    Auto small-caps InlineKeyboardButton

    Example:
        BTN("Next ▶", callback_data="next")
    """
    return InlineKeyboardButton(
        small_caps(str(text)),
        **kwargs
    )
