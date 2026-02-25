"""
utils/cyber_index.py — MULTI-DB EDITION + TMDB LANGUAGE FALLBACK
==================================================================
Parses a Pyrogram Message (document / video / audio) and saves it
as a structured file_doc into:
  • Selected MongoDB file DB(s)  (target = "db1" | "db2" | "both")
  • SQLite                        (always)

CODEC FIX (this version):
  - episode_exists check now also looks at the existing row's codec field.
    If the existing row has codec='' and the new file has a codec, we let
    the upsert through so save_file_to_sql_sync can update the codec column.
  - _parse_codec() patterns tightened with word-boundary guards to prevent
    false positives (e.g. "AVC" matching inside "INAVCODED").
"""

import re
import asyncio
from DB.database import save_file_to_db, episode_exists, get_global_setting
from config import logger


# ─────────────────────────────────────────────────────────────
# REGEX PATTERNS
# ─────────────────────────────────────────────────────────────

_SE_RE = re.compile(r"[sS](\d{1,3})[._\-\s]*[eE](\d{1,3})", re.IGNORECASE)
_SEASON_ONLY_RE = re.compile(
    r"(?:season[\s._\-]*(\d{1,3})|[sS](\d{1,3})(?![eE]))", re.IGNORECASE
)
_EP_ONLY_RE = re.compile(
    r"(?:[eE][pP]?|[eE]pisode[\s._\-]*)(\d{1,3})", re.IGNORECASE
)

_QUALITY_MAP = [
    (re.compile(r"\b4[kK]\b"),      "4K"),
    (re.compile(r"\b2160[pP]\b"),   "4K"),
    (re.compile(r"\b1080[pP]\b"),   "1080P"),
    (re.compile(r"\b720[pP]\b"),    "720P"),
    (re.compile(r"\b480[pP]\b"),    "480P"),
    (re.compile(r"\b360[pP]\b"),    "360P"),
]

# CODEC FIX: added \b word-boundary guards to avoid false positives
_CODEC_MAP = [
    (re.compile(r"H\.?265|\bHEVC\b|\bx265\b",         re.IGNORECASE), "H.265"),
    (re.compile(r"H\.?264|\bAVC\b|\bx264\b",          re.IGNORECASE), "H.264"),
    (re.compile(r"\bAV1\b",                            re.IGNORECASE), "AV1"),
    (re.compile(r"\bVP9\b",                            re.IGNORECASE), "VP9"),
    (re.compile(r"\bHDR\b|\bDOVI\b|Dolby.?Vision",    re.IGNORECASE), "HDR"),
]

_LANG_MAP = [
    (re.compile(r"\bHINDI\b"),      "Hindi"),
    (re.compile(r"\bENGLISH\b"),    "English"),
    (re.compile(r"\bTAMIL\b"),      "Tamil"),
    (re.compile(r"\bTELUGU\b"),     "Telugu"),
    (re.compile(r"\bMALAYALAM\b"),  "Malayalam"),
    (re.compile(r"\bKANNADA\b"),    "Kannada"),
    (re.compile(r"\bBENGALI\b"),    "Bengali"),
    (re.compile(r"\bMARATHI\b"),    "Marathi"),
    (re.compile(r"\bPUNJABI\b"),    "Punjabi"),
    (re.compile(r"\bKOREAN\b"),     "Korean"),
    (re.compile(r"\bJAPANESE\b"),   "Japanese"),
    (re.compile(r"\bCHINESE\b"),    "Chinese"),
    (re.compile(r"\bMANDARIN\b"),   "Chinese"),
    (re.compile(r"\bSPANISH\b"),    "Spanish"),
    (re.compile(r"\bFRENCH\b"),     "French"),
    (re.compile(r"\bGERMAN\b"),     "German"),
    (re.compile(r"\bARABIC\b"),     "Arabic"),
    (re.compile(r"\bTURKISH\b"),    "Turkish"),
    (re.compile(r"\bITALIAN\b"),    "Italian"),
    (re.compile(r"\bPORTUGUESE\b"), "Portuguese"),
    (re.compile(r"\bRUSSIAN\b"),    "Russian"),
    (re.compile(r"\bTHAI\b"),       "Thai"),
    (re.compile(r"\bHIN\b"),        "Hindi"),
    (re.compile(r"\bENG\b"),        "English"),
    (re.compile(r"\bTAM\b"),        "Tamil"),
    (re.compile(r"\bTEL\b"),        "Telugu"),
    (re.compile(r"\bMAL\b"),        "Malayalam"),
    (re.compile(r"\bKAN\b"),        "Kannada"),
    (re.compile(r"\bBEN\b"),        "Bengali"),
    (re.compile(r"\bMAR\b"),        "Marathi"),
    (re.compile(r"\bPUN\b"),        "Punjabi"),
    (re.compile(r"\bKOR\b"),        "Korean"),
    (re.compile(r"\bJPN\b"),        "Japanese"),
    (re.compile(r"\bJAP\b"),        "Japanese"),
    (re.compile(r"\bCHN\b"),        "Chinese"),
    (re.compile(r"\bCHI\b"),        "Chinese"),
    (re.compile(r"\bESP\b"),        "Spanish"),
    (re.compile(r"\bFRE\b"),        "French"),
    (re.compile(r"\bGER\b"),        "German"),
    (re.compile(r"\bARA\b"),        "Arabic"),
    (re.compile(r"\bTUR\b"),        "Turkish"),
    (re.compile(r"\bITA\b"),        "Italian"),
    (re.compile(r"\bPOR\b"),        "Portuguese"),
    (re.compile(r"\bRUS\b"),        "Russian"),
]

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
_PART_RE      = re.compile(r"\b(?:part|pt)[\s._\-]*\d+\b", re.IGNORECASE)
_BARE_NUM_RE  = re.compile(r"\b\d+\b")


# ─────────────────────────────────────────────────────────────
# PARSERS
# ─────────────────────────────────────────────────────────────

def _parse_season_episode(name: str) -> tuple[int, int]:
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


def _parse_quality(name: str, caption: str = "") -> str:
    combined = f"{name} {caption}"
    for pattern, label in _QUALITY_MAP:
        if pattern.search(combined):
            return label
    return "Unknown"


def _quality_from_resolution(width: int, height: int) -> str:
    long_side = max(width or 0, height or 0)
    if long_side >= 2160:
        return "4K"
    if long_side >= 1080:
        return "1080P"
    if long_side >= 720:
        return "720P"
    if long_side >= 480:
        return "480P"
    if long_side > 0:
        return "360P"
    return "Unknown"


def _parse_codec(name: str, caption: str = "") -> str:
    """
    Detect codec from filename and caption.
    Returns e.g. "H.265", "H.264", "AV1" or "" if unknown.
    Word-boundary guards prevent false positives.
    """
    combined = f"{name} {caption}"
    for pattern, label in _CODEC_MAP:
        if pattern.search(combined):
            return label
    return ""


def _parse_language(name: str, caption: str = "") -> str:
    combined     = f"{name} {caption}"
    upper        = combined.upper()
    upper_norm   = re.sub(r"[._\-+&/\\,]+", " ", upper)
    bracket_raw  = " ".join(re.findall(r"[\[\({]([^\]\){}]+)[\]\){}]", upper))
    bracket_norm = re.sub(r"[._\-+&/\\,]+", " ", bracket_raw)
    search       = f"{bracket_norm} {upper_norm}"

    found = []
    seen  = set()
    for pattern, label in _LANG_MAP:
        if label not in seen and pattern.search(search):
            found.append(label)
            seen.add(label)

    is_multi = bool(re.search(r"\b(DUAL|MULTI)\b", search))

    if is_multi:
        if len(found) >= 2:
            return " + ".join(found)
        return "Multi Audio"

    if not found:
        return "Unknown"
    if len(found) == 1:
        return found[0]
    return " + ".join(found)


# ─────────────────────────────────────────────────────────────
# TMDB / OMDB LANGUAGE FALLBACK
# ─────────────────────────────────────────────────────────────

_LANG_CACHE: dict[str, str] = {}


async def _get_language_from_api(normalized_name: str) -> str:
    if not normalized_name:
        return "Unknown"

    key = normalized_name.lower().strip()

    if key in _LANG_CACHE:
        return _LANG_CACHE[key]

    try:
        from utils.cyber_poster import get_cached
        cached = await asyncio.to_thread(get_cached, key)
        if cached:
            lang = cached[5] if cached[5] else None
            if lang and lang not in ("Original Language", "Unknown", ""):
                _LANG_CACHE[key] = lang
                return lang
    except Exception as e:
        logger.debug(f"[LANG] SQLite lookup failed for {key!r}: {e}")

    try:
        from CyberTG.poster import fetch_tmdb, fetch_omdb
        from utils.cyber_poster import save_cache

        tmdb = await fetch_tmdb(key)
        if tmdb:
            poster, year, genre, rating, lang = tmdb
            if lang and lang not in ("Original Language", "Unknown", ""):
                await asyncio.to_thread(
                    save_cache, key, poster, year, genre, rating, "tmdb", lang
                )
                _LANG_CACHE[key] = lang
                return lang

        omdb = await fetch_omdb(key)
        if omdb:
            poster, year, genre, rating, lang = omdb
            if lang and lang not in ("Original Language", "Unknown", ""):
                await asyncio.to_thread(
                    save_cache, key, poster, year, genre, rating, "omdb", lang
                )
                _LANG_CACHE[key] = lang
                return lang

    except Exception as e:
        logger.debug(f"[LANG] API lookup failed for {key!r}: {e}")

    _LANG_CACHE[key] = "Unknown"
    return "Unknown"


# ─────────────────────────────────────────────────────────────
# SERIES NAME NORMALIZER
# ─────────────────────────────────────────────────────────────

def _normalized_series_name(file_name: str) -> str:
    if not file_name:
        return ""

    name = file_name
    name = re.sub(r"\.(mkv|mp4|avi|mov|wmv|flv|webm)$", "", name, flags=re.IGNORECASE)
    name = _MENTION_RE.sub(" ", name)

    cut_match = (
        _SE_RE.search(name)
        or _SEASON_ONLY_RE.search(name)
        or _EP_ONLY_RE.search(name)
    )
    base = name[: cut_match.start()] if cut_match else name

    base = _BRACKET_RE.sub(" ", base)
    base = _CLEAN_SEP_RE.sub(" ", base)
    base = _YEAR_RE.sub(" ", base)
    base = _PART_RE.sub(" ", base)
    base = _JUNK_RE.sub(" ", base)
    base = _NON_WORD_RE.sub(" ", base)
    base = _BARE_NUM_RE.sub(" ", base)
    base = _MULTI_SPC_RE.sub(" ", base).strip()

    words = base.split()
    while words and __import__("re").fullmatch(r"[A-Z]{1,4}", words[0]):
        words.pop(0)
    base = " ".join(words)

    base = re.sub(r"\b[a-zA-Z]{1,2}$", "", base).strip()
    parts = base.split()
    if len(parts) > 4:
        parts = parts[:4]

    return " ".join(parts).title()


# ─────────────────────────────────────────────────────────────
# MEDIA TYPE DETECTOR
# ─────────────────────────────────────────────────────────────

def _get_media_info(msg):
    if msg.video:
        return msg.video, "video"
    if msg.audio:
        return msg.audio, "audio"
    if msg.document:
        return msg.document, "document"
    return None, None


# ─────────────────────────────────────────────────────────────
# CODEC-AWARE DUPLICATE CHECK
# ─────────────────────────────────────────────────────────────

async def _episode_exists_with_codec_check(
    normalized_name: str,
    season: int,
    episode: int,
    quality: str,
    language: str,
    new_codec: str,
) -> bool:
    """
    CODEC FIX: Extended duplicate check.

    If the episode already exists BUT its stored codec is empty AND
    the new file has a detected codec, return False so the upsert runs
    and backfills the codec column.

    In all other cases behaves identically to episode_exists().
    """
    from DB.sqlite_db import get_file_by_message_id_and_channel_id_sync, _get_conn

    # Standard existence check first
    exists = await episode_exists(normalized_name, season, episode, quality, language)
    if not exists:
        return False   # not a duplicate — save normally

    if not new_codec:
        return True    # exists and we have nothing new to add — skip

    # Episode exists and we have a codec — check if existing row is missing it
    from DB.sqlite_db import sql_db_lock as _sql_lock
    import asyncio as _asyncio

    def _check_existing_codec():
        conn   = _get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT codec FROM files
            WHERE normalized_name = ?
              AND season   = ?
              AND episode  = ?
              AND quality  = ?
              AND language = ?
            LIMIT 1
        """, (normalized_name.lower(), season, episode, quality, language))
        row = cursor.fetchone()
        if row is None:
            return ""
        return row[0] or ""

    from DB.sqlite_db import sql_db_lock
    async with sql_db_lock:
        existing_codec = await _asyncio.to_thread(_check_existing_codec)

    if not existing_codec:
        # Existing row has no codec — let the upsert through to fill it in
        logger.debug(
            f"[cyber_index] Codec backfill: {normalized_name!r} "
            f"S{season:02d}E{episode:02d} {quality} [{language}] "
            f"existing_codec='' new_codec={new_codec!r} → allowing upsert"
        )
        return False

    # Existing row already has a codec — genuine duplicate, skip
    return True


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

    Returns
    -------
    (success: bool, count: int)
    """
    try:
        file_obj, media_type = _get_media_info(msg)
        if not file_obj:
            return False, 0

        file_name = getattr(file_obj, "file_name", None)
        if not file_name:
            return False, 0

        file_name = file_name.strip()
        if not file_name:
            return False, 0

        channel_id = msg.chat.id if msg.chat else None
        message_id = msg.id

        if not channel_id:
            return False, 0

        caption = getattr(msg, "caption", None) or ""
        if not isinstance(caption, str):
            caption = str(caption)

        season, episode = _parse_season_episode(file_name)
        quality         = _parse_quality(file_name, caption)
        codec           = _parse_codec(file_name, caption)

        use_caption_lang = await get_global_setting("CAPTION_LANG", True)
        lang_caption     = caption if use_caption_lang else ""
        language         = _parse_language(file_name, lang_caption)
        normalized_name  = _normalized_series_name(file_name)

        if not normalized_name:
            logger.debug(f"[cyber_index] Skipped (no series name): {file_name}")
            return False, 0

        # Resolution fallback for quality
        if quality == "Unknown" and media_type == "video":
            width  = getattr(file_obj, "width",  0) or 0
            height = getattr(file_obj, "height", 0) or 0
            if width or height:
                quality = _quality_from_resolution(width, height)

        # Language fallback via TMDB/OMDB
        if language == "Unknown":
            language = await _get_language_from_api(normalized_name)

        # CODEC FIX: use codec-aware duplicate check instead of plain episode_exists
        norm_lower = normalized_name.lower()
        is_dup = await _episode_exists_with_codec_check(
            norm_lower, season, episode, quality, language, codec
        )
        if is_dup:
            logger.debug(
                f"[cyber_index] ⏭ Already exists: {normalized_name!r} "
                f"S{season:02d}E{episode:02d} {quality} [{language}] — skipped"
            )
            return False, 0

        file_doc = {
            "channel_id":      channel_id,
            "message_id":      message_id,
            "file_name":       file_name,
            "normalized_name": norm_lower,
            "season":          season,
            "episode":         episode,
            "quality":         quality,
            "language":        language,
            "file_id":         file_obj.file_id,
            "media_type":      media_type,
            "caption":         caption,
            "codec":           codec,
        }

        saved = await save_file_to_db(file_doc, target=target)

        if saved:
            logger.debug(
                f"[cyber_index] ✅ Saved: {normalized_name!r} "
                f"S{season:02d}E{episode:02d} {quality} [{language}] codec={codec!r} → {target}"
            )
            return True, 1

        return False, 0

    except Exception as e:
        logger.error(f"[cyber_index] save_series_file_for_bulk_index error: {e}")
        return False, 0


# ─────────────────────────────────────────────────────────────
# STANDALONE PARSE HELPER
# ─────────────────────────────────────────────────────────────

def parse_file_name(file_name: str, caption: str = "") -> dict:
    """Returns parsed metadata dict without saving."""
    season, episode = _parse_season_episode(file_name)
    return {
        "file_name":       file_name,
        "normalized_name": _normalized_series_name(file_name),
        "season":          season,
        "episode":         episode,
        "quality":         _parse_quality(file_name, caption),
        "language":        _parse_language(file_name, caption),
        "codec":           _parse_codec(file_name, caption),
    }
