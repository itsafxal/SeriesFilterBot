"""
utils/cyber_index.py — MULTI-DB EDITION + TMDB LANGUAGE FALLBACK
==================================================================
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

LANGUAGE DETECTION PRIORITY:
  1. Filename tags     (e.g. "Hindi", "HIN", "[Tamil]")
  2. Caption text      (e.g. "Language: Hindi", "Audio: Tamil")
  3. TMDB/OMDB lookup  (original_language of the series — cached in SQLite)
  4. "Unknown"         (only if all above fail — never assumes English)
"""

import re
import asyncio
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

# Language patterns — ordered: full words first (most reliable), then 3-letter codes.
# NOTE: 2-letter codes like HI / EN removed — too many false positives
#       (HI matches "HI-DEF", EN matches "NETFLIX", etc.)
_LANG_MAP = [
    # ── Full words ───────────────────────────────────────────────
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
    # ── 3-letter codes ───────────────────────────────────────────
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


def _parse_language(name: str, caption: str = "") -> str:
    """
    Detect language from filename AND caption text.

    Strategy:
      1. Combine filename + caption for maximum signal.
      2. Search bracket/parenthesis content first (highest confidence).
      3. Then search full normalized text.
      4. Full words matched before 3-letter codes to avoid partial hits.
      5. Default is "Unknown" (not "English") — honest when no tag found.

    Args:
        name:    raw file_name string
        caption: Telegram message caption (optional, but greatly helps accuracy)
    """
    # ── Build search space ────────────────────────────────────────────────
    combined = f"{name} {caption}"
    upper    = combined.upper()

    # Normalise separators: dots, dashes, underscores, plus, &, / → space
    upper_norm = re.sub(r"[._\-+&/\\,]+", " ", upper)

    # Also extract bracket / paren content separately — high confidence source
    # e.g. "[Hindi]", "(Tamil + English)", "{HIN+ENG}"
    bracket_raw     = " ".join(re.findall(r"[\[\({]([^\]\){}]+)[\]\){}]", upper))
    bracket_norm    = re.sub(r"[._\-+&/\\,]+", " ", bracket_raw)

    # Final search space: bracket content prepended (searched first via ordering)
    search = f"{bracket_norm} {upper_norm}"

    # ── Match languages ───────────────────────────────────────────────────
    found = []
    seen  = set()
    for pattern, label in _LANG_MAP:
        if label not in seen and pattern.search(search):
            found.append(label)
            seen.add(label)

    # ── Multi / Dual audio ────────────────────────────────────────────────
    is_multi = bool(re.search(r"\b(DUAL|MULTI)\b", search))

    if is_multi:
        if len(found) >= 2:
            return " + ".join(found)
        return "Multi Audio"

    if not found:
        return "Unknown"       # honest — do NOT assume English

    if len(found) == 1:
        return found[0]

    return " + ".join(found)   # e.g. "Hindi + English"


# ─────────────────────────────────────────────────────────────
# TMDB / OMDB LANGUAGE FALLBACK  (called only when filename
# and caption both fail to identify a language)
#
# Design:
#   • Uses the SAME SQLite cache as poster.py — zero extra DB calls
#     if the series was already looked up during a search/browse.
#   • If not cached: queries TMDB first, then OMDB as fallback.
#   • Result is cached so subsequent files of the same series
#     (bulk index) cost 0 API calls after the first one.
#   • Runs in asyncio.to_thread for the sync SQLite reads so it
#     doesn't block the event loop in RTI.
#   • Returns "Unknown" (never crashes) if both APIs fail.
# ─────────────────────────────────────────────────────────────

# In-process memory cache: normalized_name → language string
# Prevents repeated SQLite lookups for the same series during
# a single bulk-index run (e.g. 500 episodes of Mirzapur).
_LANG_CACHE: dict[str, str] = {}


async def _get_language_from_api(normalized_name: str) -> str:
    """
    Resolve language for `normalized_name` via:
      memory cache → SQLite cache → TMDB API → OMDB API → "Unknown"

    This is intentionally a module-level async function so both
    save_series_file_for_bulk_index() and external callers can use it.
    """
    if not normalized_name:
        return "Unknown"

    key = normalized_name.lower().strip()

    # ── 1. Memory (fastest — no I/O at all) ──────────────────
    if key in _LANG_CACHE:
        return _LANG_CACHE[key]

    # ── 2. SQLite cache (same DB used by poster.py) ──────────
    try:
        from utils.cyber_poster import get_cached
        cached = await asyncio.to_thread(get_cached, key)
        if cached:
            # cached tuple: (poster, year, genre, rating, source, language)
            lang = cached[5] if cached[5] else None
            if lang and lang not in ("Original Language", "Unknown", ""):
                _LANG_CACHE[key] = lang
                logger.debug(f"[LANG] SQLite cache hit: {key!r} → {lang}")
                return lang
    except Exception as e:
        logger.debug(f"[LANG] SQLite lookup failed for {key!r}: {e}")

    # ── 3. TMDB API ───────────────────────────────────────────
    try:
        from CyberTG.poster import fetch_tmdb, fetch_omdb
        from utils.cyber_poster import save_cache

        tmdb = await fetch_tmdb(key)
        if tmdb:
            poster, year, genre, rating, lang = tmdb
            if lang and lang not in ("Original Language", "Unknown", ""):
                # Persist to SQLite so future lookups are instant
                await asyncio.to_thread(
                    save_cache, key, poster, year, genre, rating, "tmdb", lang
                )
                _LANG_CACHE[key] = lang
                logger.debug(f"[LANG] TMDB resolved: {key!r} → {lang}")
                return lang

        # ── 4. OMDB API (fallback) ────────────────────────────
        omdb = await fetch_omdb(key)
        if omdb:
            poster, year, genre, rating, lang = omdb
            if lang and lang not in ("Original Language", "Unknown", ""):
                await asyncio.to_thread(
                    save_cache, key, poster, year, genre, rating, "omdb", lang
                )
                _LANG_CACHE[key] = lang
                logger.debug(f"[LANG] OMDB resolved: {key!r} → {lang}")
                return lang

    except Exception as e:
        logger.debug(f"[LANG] API lookup failed for {key!r}: {e}")

    # ── 5. Truly unknown ─────────────────────────────────────
    _LANG_CACHE[key] = "Unknown"
    return "Unknown"


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
        # Extract caption — channels often include language info here
        caption = getattr(msg, "caption", None) or ""
        if not isinstance(caption, str):
            caption = str(caption)

        season, episode = _parse_season_episode(file_name)
        quality         = _parse_quality(file_name)
        language        = _parse_language(file_name, caption)  # caption = extra signal
        normalized_name = _normalized_series_name(file_name)

        if not normalized_name:
            logger.debug(f"[cyber_index] Skipped (no series name): {file_name}")
            return False, 0

        # ── TMDB / OMDB language fallback ──────────────────────
        # If filename + caption both failed to detect language,
        # resolve via TMDB/OMDB using the series title.
        # Result is SQLite-cached so bulk index costs 1 API call
        # per unique series, not 1 per file.
        if language == "Unknown":
            language = await _get_language_from_api(normalized_name)
            if language != "Unknown":
                logger.debug(
                    f"[cyber_index] API resolved lang: {normalized_name!r} → {language}"
                )

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
            "caption":         caption,                   # ← saved for downstream use
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
        "language":        _parse_language(file_name),  # no caption in standalone mode
    }
