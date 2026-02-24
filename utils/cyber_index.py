"""
utils/cyber_index.py — MULTI-DB EDITION
=========================================
Parses a Pyrogram Message (document / video / audio) and saves it
as a structured file_doc into:
  • Selected MongoDB file DB(s)  (target = "db1" | "db2" | "both")
  • SQLite                        (always)

File-doc fields:
  channel_id      int   — source channel
  message_id      int   — Telegram message ID
  file_name       str   — original filename
  normalized_name str   — cleaned series title for search
  season          int   — parsed season number (0 if unknown)
  episode         int   — parsed episode number (0 if unknown)
  quality         str   — "4K" | "1080P" | "720P" | "480P" | "360P" | "Unknown"
  language        str   — detected language string
  file_id         str   — Telegram file_id for re-sending
  media_type      str   — "video" | "audio" | "document"
"""

import re
from DB.database import save_file_to_db
from config import logger


# ─────────────────────────────────────────────────────────────
# REGEX PATTERNS
# ─────────────────────────────────────────────────────────────

# Season + Episode: S01E02, S1E2, s01.e02, s01-e02, s01 e02
_SE_RE = re.compile(
    r"[sS](\d{1,3})[._\-\s]*[eE](\d{1,3})",
    re.IGNORECASE,
)

# Season-only fallback: Season 2, S02
_SEASON_ONLY_RE = re.compile(
    r"(?:season[\s._\-]*(\d{1,3})|[sS](\d{1,3})(?![eE]))",
    re.IGNORECASE,
)

# Episode-only fallback: E04, Ep04, Episode 4
_EP_ONLY_RE = re.compile(
    r"(?:[eE][pP]?|[eE]pisode[\s._\-]*)(\d{1,3})",
    re.IGNORECASE,
)

# Quality tokens
_QUALITY_MAP = [
    (re.compile(r"\b4[kK]\b"),                          "4K"),
    (re.compile(r"\b2160[pP]\b"),                       "4K"),
    (re.compile(r"\b1080[pP]\b"),                       "1080P"),
    (re.compile(r"\b720[pP]\b"),                        "720P"),
    (re.compile(r"\b480[pP]\b"),                        "480P"),
    (re.compile(r"\b360[pP]\b"),                        "360P"),
]

# Language patterns — checked against full upper-cased filename
_LANG_MAP = [
    (re.compile(r"\b(HINDI|HIN|HI)\b"),               "Hindi"),
    (re.compile(r"\b(ENGLISH|ENG|EN)\b"),             "English"),
    (re.compile(r"\b(TAMIL|TAM)\b"),                  "Tamil"),
    (re.compile(r"\b(TELUGU|TEL)\b"),                 "Telugu"),
    (re.compile(r"\b(MALAYALAM|MAL)\b"),              "Malayalam"),
    (re.compile(r"\b(KANNADA|KAN)\b"),                "Kannada"),
    (re.compile(r"\b(BENGALI|BEN)\b"),                "Bengali"),
    (re.compile(r"\b(MARATHI|MAR)\b"),                "Marathi"),
    (re.compile(r"\b(PUNJABI|PUN)\b"),                "Punjabi"),
    (re.compile(r"\b(KOREAN|KOR)\b"),                 "Korean"),
    (re.compile(r"\b(JAPANESE|JPN|JAP)\b"),           "Japanese"),
    (re.compile(r"\b(CHINESE|CHN|CHI)\b"),            "Chinese"),
    (re.compile(r"\b(SPANISH|ESP)\b"),                "Spanish"),
    (re.compile(r"\b(FRENCH|FRE)\b"),                 "French"),
    (re.compile(r"\b(GERMAN|GER)\b"),                 "German"),
    (re.compile(r"\b(ARABIC|ARA)\b"),                 "Arabic"),
]

# Junk tokens to strip when building normalized_name
_JUNK_RE = re.compile(r"""
    \b(
    4k|2160p|1080p|720p|480p|360p|
    bluray|blu\-ray|bdrip|bdremux|
    webrip|webdl|web\-dl|web|hdrip|hq|hdtv|
    x264|x265|hevc|avc|h264|h265|
    aac|ac3|dts|ddp|ddp5\.1|mp3|opus|
    10bit|8bit|hdr|sdr|dovi|dolby|
    amzn|netflix|prime|hotstar|hulu|disney|hbo|sony|
    tgx|yts|rip|rz|strife|psa|repack|proper|
    subs|sub|dubbed|dub|multi|dual|
    complete|full|batch|pack|collection|set|
    vol|volume|episode|ep|season|
    mkv|mp4|avi|mov
    )\b
""", re.IGNORECASE | re.VERBOSE)

_CLEAN_SEP_RE = re.compile(r"[._\-]+")
_BRACKET_RE   = re.compile(r"\[.*?\]|\(.*?\)|{.*?}")
_MENTION_RE   = re.compile(r"@\S+")
_YEAR_RE      = re.compile(r"\b(19|20)\d{2}\b")
_NON_WORD_RE  = re.compile(r"[^a-zA-Z0-9\s]")
_MULTI_SPC_RE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────
# PARSERS
# ─────────────────────────────────────────────────────────────

def _parse_season_episode(name: str) -> tuple[int, int]:
    """Return (season, episode). Falls back to 0 when not found."""
    m = _SE_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))

    season  = 0
    episode = 0

    ms = _SEASON_ONLY_RE.search(name)
    if ms:
        season = int(ms.group(1) or ms.group(2))

    me = _EP_ONLY_RE.search(name)
    if me:
        episode = int(me.group(1))

    return season, episode


def _parse_quality(name: str) -> str:
    for pattern, label in _QUALITY_MAP:
        if pattern.search(name):
            return label
    return "Unknown"


def _parse_language(name: str) -> str:
    upper = name.upper()
    # Normalise separators so "HIN+ENG" → "HIN ENG"
    upper = re.sub(r"[+&/x,.\-_]+", " ", upper)

    found = []
    for pattern, label in _LANG_MAP:
        if pattern.search(upper):
            found.append(label)

    if not found:
        if re.search(r"\b(DUAL|MULTI)\b", upper):
            return "Multi Audio"
        return "English"   # safe default

    if len(found) == 1:
        return found[0]

    # e.g. "Hindi-English" for dual dubs
    return "-".join(found)


def _normalized_series_name(file_name: str) -> str:
    """
    Strips everything AFTER the SxxExx marker, then cleans junk
    to leave only the series title words.
    """
    if not file_name:
        return ""

    name = file_name

    # Remove extension
    name = re.sub(r"\.(mkv|mp4|avi|mov|wmv|flv|webm)$", "", name, flags=re.IGNORECASE)

    # Strip @mentions
    name = _MENTION_RE.sub(" ", name)

    # Cut at SxxExx
    m = _SE_RE.search(name)
    base = name[: m.start()] if m else name

    # Brackets
    base = _BRACKET_RE.sub(" ", base)

    # Separators → spaces
    base = _CLEAN_SEP_RE.sub(" ", base)

    # Year
    base = _YEAR_RE.sub(" ", base)

    # Junk tokens
    base = _JUNK_RE.sub(" ", base)

    # Non-word chars
    base = _NON_WORD_RE.sub(" ", base)

    # Collapse
    base = _MULTI_SPC_RE.sub(" ", base).strip()

    # Drop trailing 1-2 char stubs
    base = re.sub(r"\b[a-zA-Z]{1,2}$", "", base).strip()

    # Title-case and cap at 4 words (enough for series titles)
    parts = base.split()
    if len(parts) > 4:
        parts = parts[:4]

    return " ".join(parts).title()


# ─────────────────────────────────────────────────────────────
# MEDIA TYPE DETECTOR
# ─────────────────────────────────────────────────────────────

def _get_media_info(msg):
    """
    Returns (file_obj, media_type_str) from a Pyrogram Message.
    Prefers video > audio > document.
    """
    if msg.video:
        return msg.video, "video"
    if msg.audio:
        return msg.audio, "audio"
    if msg.document:
        return msg.document, "document"
    return None, None


# ─────────────────────────────────────────────────────────────
# MAIN PUBLIC FUNCTION
# ─────────────────────────────────────────────────────────────

async def save_series_file_for_bulk_index(
    msg,
    target: str = "both",
) -> tuple[bool, int]:
    """
    Parse a Pyrogram message and save the file to the DB(s).

    Parameters
    ----------
    msg    : pyrogram.types.Message
    target : "db1" | "db2" | "both"
             Which MongoDB file DB(s) to write to.
             SQLite is always written.

    Returns
    -------
    (success: bool, count: int)
        count is 1 if saved, 0 if skipped/failed.
    """
    try:
        file_obj, media_type = _get_media_info(msg)
        if not file_obj:
            return False, 0

        # ── File name ──────────────────────────────────────────
        file_name = getattr(file_obj, "file_name", None)
        if not file_name:
            # Video/audio without file_name → skip (no metadata to parse)
            return False, 0

        file_name = file_name.strip()
        if not file_name:
            return False, 0

        # ── Channel / message IDs ──────────────────────────────
        channel_id = msg.chat.id if msg.chat else None
        message_id = msg.id

        if not channel_id:
            return False, 0

        # ── Parse metadata ─────────────────────────────────────
        season, episode = _parse_season_episode(file_name)
        quality         = _parse_quality(file_name)
        language        = _parse_language(file_name)
        normalized_name = _normalized_series_name(file_name)

        if not normalized_name:
            logger.debug(f"[cyber_index] Skipped (no series name): {file_name}")
            return False, 0

        # ── Build document ─────────────────────────────────────
        file_doc = {
            "channel_id":      channel_id,
            "message_id":      message_id,
            "file_name":       file_name,
            "normalized_name": normalized_name.lower(),   # stored lower for search
            "season":          season,
            "episode":         episode,
            "quality":         quality,
            "language":        language,
            "file_id":         file_obj.file_id,
            "media_type":      media_type,
        }

        # ── Save ───────────────────────────────────────────────
        saved = await save_file_to_db(file_doc, target=target)

        if saved:
            logger.debug(
                f"[cyber_index] ✅ Saved: {normalized_name!r} "
                f"S{season:02d}E{episode:02d} {quality} [{language}] → {target}"
            )
            return True, 1

        # Already exists (UNIQUE constraint) — not an error
        return False, 0

    except Exception as e:
        logger.error(f"[cyber_index] save_series_file_for_bulk_index error: {e}")
        return False, 0


# ─────────────────────────────────────────────────────────────
# STANDALONE PARSE HELPER  (used by command.py stats / admin)
# ─────────────────────────────────────────────────────────────

def parse_file_name(file_name: str) -> dict:
    """
    Returns parsed metadata dict without saving.
    Useful for previewing what would be indexed.
    """
    season, episode = _parse_season_episode(file_name)
    return {
        "file_name":       file_name,
        "normalized_name": _normalized_series_name(file_name),
        "season":          season,
        "episode":         episode,
        "quality":         _parse_quality(file_name),
        "language":        _parse_language(file_name),
    }
