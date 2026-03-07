from pyrogram.types import InlineKeyboardButton

SMALL_CAPS_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ",
    "e": "ᴇ", "f": "ғ", "g": "ɢ", "h": "ʜ",
    "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ",
    "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ",
    "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ",
    "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ"
}

def small_caps(text: str):
    if not text:
        return ""

    return "".join(
        SMALL_CAPS_MAP.get(ch.lower(), ch)
        for ch in str(text)
    )

def UI(text: str):
    """
    Auto UI text styling.
    """
    return small_caps(text)

def BTN(text, **kwargs):
    """
    Auto small-caps InlineKeyboardButton with live-button spacing.
    Leading emoji/symbols are preserved; text after gets small-caps.

    Example:
        BTN("▶  Season 01", callback_data="s1")
    """
    t = str(text)
    # Split on first double-space so "⬇  Download" → emoji preserved, text gets caps
    if "  " in t:
        prefix, rest = t.split("  ", 1)
        label = prefix + "  " + small_caps(rest)
    else:
        label = small_caps(t)
    return InlineKeyboardButton(label, **kwargs)
