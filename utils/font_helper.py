
SMALL_CAPS_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ",
    "e": "ᴇ", "f": "ғ", "g": "ɢ", "h": "ʜ",
    "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ",
    "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ",
    "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ",
    "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ"
}


def small_caps(text: str) -> str:
    """Convert text to small caps style."""
    if not text:
        return ""

    out = ""
    for ch in text:
        low = ch.lower()
        out += SMALL_CAPS_MAP.get(low, ch)

    return out
