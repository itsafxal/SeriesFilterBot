"""
utils/cyber_index.py — FIXED: year pattern no longer treated as season/episode
"""

import re
import asyncio
from DB.database import save_file_to_db, episode_exists, get_global_setting, get_caption_clean_keywords
from config import logger

# ── Caption clean cache ──────────────────────────────────────
_CLEAN_KW_CACHE: list | None = None

def set_clean_keywords_cache(keywords: list | None):
    global _CLEAN_KW_CACHE
    _CLEAN_KW_CACHE = keywords

async def _get_clean_keywords() -> list:
    if _CLEAN_KW_CACHE is not None:
        return _CLEAN_KW_CACHE
    return await get_caption_clean_keywords()

def _apply_clean(text: str, keywords: list) -> str:
    if not text or not keywords:
        return text
    for kw in keywords:
        if kw:
            text = text.replace(kw, "")
    return text.strip()

# ── Full pattern library ─────────────────────────────────────
PATTERNS = {
    "S01E01": re.compile(r"\b[sS](\d{1,3})[.\-_\s]?[eE](\d{1,3})\b", re.IGNORECASE),
    "01x01": re.compile(r"\b(\d{1,3})[xX](\d{1,3})\b", re.IGNORECASE),
    "Season Episode": re.compile(r"\bseason[.\-_\s]*(\d{1,3})[.\-_\s]*episode[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Season Ep": re.compile(r"\bseason[.\-_\s]*(\d{1,3})[.\-_\s]*(?:ep|e)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "S01 EP01": re.compile(r"\b[sS](\d{1,3})[.\-_\s]*(?:ep|e)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "S01-E01": re.compile(r"\b[sS](\d{1,3})-(?:[eE])(\d{1,3})\b", re.IGNORECASE),
    "S01.E01": re.compile(r"\b[sS](\d{1,3})\.(?:[eE])(\d{1,3})\b", re.IGNORECASE),
    "S01_E01": re.compile(r"\b[sS](\d{1,3})_(?:[eE])(\d{1,3})\b", re.IGNORECASE),
    "S01 E01": re.compile(r"\b[sS](\d{1,3})\s+(?:[eE])(\d{1,3})\b", re.IGNORECASE),
    "S01E01E02": re.compile(r"\b[sS](\d{1,3})[.\-_\s]*[eE](\d{1,3})(?:[eE](\d{1,3}))+"),
    "S01E01-E05": re.compile(r"\b[sS](\d{1,3})[.\-_\s]*[eE](\d{1,3})[\-~to]+(?:[eE])?(\d{1,3})"),
    "01x01-01x05": re.compile(r"\b(\d{1,3})[xX](\d{1,3})[\-~](\d{1,3})[xX](\d{1,3})\b"),
    "101": re.compile(r"\b([1-9])(\d{2})\b"),
    "1001": re.compile(r"\b(\d{2})(\d{2})\b"),
    "0101": re.compile(r"\b(\d{2})(\d{2})\b"),
    "Episode 01": re.compile(r"\bepisode[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Ep01": re.compile(r"\bep[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "E01": re.compile(r"\b[eE](\d{1,3})\b"),
    "Season 01": re.compile(r"\bseason[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "S01": re.compile(r"\b[sS](\d{1,3})(?![eE])\b", re.IGNORECASE),
    "Complete Season": re.compile(r"\b(?:complete|full)[.\-_\s]*season[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Season Pack": re.compile(r"\bseason[.\-_\s]*(\d{1,3})[.\-_\s]*(?:pack|collection)\b", re.IGNORECASE),
    "Batch": re.compile(r"\bbatch[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Anime 01v2": re.compile(r"\b(\d{1,3})v\d+\b", re.IGNORECASE),
    "S01E01v2": re.compile(r"\b[sS](\d{1,3})[eE](\d{1,3})v\d+\b", re.IGNORECASE),
    "Episode 01v2": re.compile(r"\b(?:ep|episode)[.\-_\s]*(\d{1,3})v\d+\b", re.IGNORECASE),
    "OVA": re.compile(r"\bOVA[.\-_\s]*(\d{1,3})?\b", re.IGNORECASE),
    "ONA": re.compile(r"\bONA[.\-_\s]*(\d{1,3})?\b", re.IGNORECASE),
    "Special": re.compile(r"\bspecial[.\-_\s]*(\d{1,3})?\b", re.IGNORECASE),
    "Part 01": re.compile(r"\b(?:part|pt)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Chapter 01": re.compile(r"\b(?:chapter|chap)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Volume": re.compile(r"\b(?:vol|volume)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "2024.01.15": re.compile(r"\b(20\d{2})[.\-_](\d{2})[.\-_](\d{2})\b"),
    "2024-01-15": re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"),
    "Mini Series Part": re.compile(r"\bmini[.\-_\s]*series[.\-_\s]*(?:part)?[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Season Roman": re.compile(r"\bseason[.\-_\s]*(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b", re.IGNORECASE),
    "WEB Episode": re.compile(r"\bweb[.\-_\s]*dl[.\-_\s]*ep[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "EP 01 END": re.compile(r"\bep?[.\-_\s]*(\d{1,3})[.\-_\s]*(?:end|finale)\b", re.IGNORECASE),
    "Disc 1": re.compile(r"\bdisc[.\-_\s]*(\d{1,2})\b", re.IGNORECASE),
    "CD1": re.compile(r"\bcd[.\-_\s]*(\d{1,2})\b", re.IGNORECASE),
    "Naruto 220": re.compile(r"\b(0?\d{1,4})\b"),
    "Episodes 1-10": re.compile(r"\bepisodes?[.\-_\s]*(\d{1,3})[\-~to]+(\d{1,3})\b", re.IGNORECASE),
    "Director Cut": re.compile(r"\bdirector'?s?[.\-_\s]*cut\b", re.IGNORECASE),
    "Extended": re.compile(r"\bextended\b", re.IGNORECASE),
    "Uncut": re.compile(r"\buncut\b", re.IGNORECASE),
}

ROMAN_TO_INT = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
}

# ── Quality, codec, language regex ───────────────────────────
_QUALITY_MAP = [
    (re.compile(r"\b4[kK]\b"),      "4K"),
    (re.compile(r"\b2160[pP]\b"),   "4K"),
    (re.compile(r"\b1080[pP]\b"),   "1080P"),
    (re.compile(r"\b720[pP]\b"),    "720P"),
    (re.compile(r"\b480[pP]\b"),    "480P"),
    (re.compile(r"\b360[pP]\b"),    "360P"),
]

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

# ── CAPTION_LANG CACHE ──────────────────────────────────────
_CAPTION_LANG_CACHE: bool | None = None

def set_caption_lang_cache(value: bool | None):
    global _CAPTION_LANG_CACHE
    _CAPTION_LANG_CACHE = value

async def _get_caption_lang_flag() -> bool:
    if _CAPTION_LANG_CACHE is not None:
        return _CAPTION_LANG_CACHE
    flag = await get_global_setting("CAPTION_LANG", False)
    return bool(flag)


# ══════════════════════════════════════════════════════════════
# PARSERS (using the comprehensive PATTERNS)
# ══════════════════════════════════════════════════════════════

def _parse_season_episode(name: str) -> tuple[int, int]:
    """
    Extract season & episode using the full PATTERNS library.
    Returns (season, episode). Unused dimension defaults to 0.
    Year‑like numbers (1900‑2099) are ignored for patterns 1001/0101.
    """
    text = name
    ORDER = [
        "S01E01E02", "S01E01-E05", "01x01-01x05",
        "S01E01", "S01-E01", "S01.E01", "S01_E01", "S01 E01",
        "S01 EP01", "Season Episode", "Season Ep", "01x01",
        "Episode 01", "Ep01", "E01",
        "Season 01", "S01", "Complete Season", "Season Pack", "Batch",
        "Anime 01v2", "S01E01v2", "Episode 01v2",
        "OVA", "ONA", "Special",
        "Part 01", "Chapter 01", "Volume",
        "2024.01.15", "2024-01-15", "Mini Series Part",
        "Season Roman", "WEB Episode", "EP 01 END", "Disc 1", "CD1",
        "Episodes 1-10",
        "101", "1001", "0101",
    ]

    season = episode = 0

    for key in ORDER:
        pat = PATTERNS.get(key)
        if not pat:
            continue
        m = pat.search(text)
        if not m:
            continue

        groups = m.groups()

        # ── YEAR CHECK for ambiguous 4‑digit patterns ─
        if key in ("1001", "0101"):
            try:
                full_num = int(m.group(0))
                if 1900 <= full_num <= 2099:
                    continue   # looks like a year → skip
            except Exception:
                pass

        # Extract based on pattern
        if key in ("S01E01", "S01-E01", "S01.E01", "S01_E01", "S01 E01",
                   "S01 EP01", "Season Episode", "Season Ep", "01x01",
                   "S01E01v2"):
            season, episode = int(groups[0]), int(groups[1])
            break
        elif key == "S01E01E02":
            season, episode = int(groups[0]), int(groups[1])
            break
        elif key == "S01E01-E05":
            season, episode = int(groups[0]), int(groups[1])
            break
        elif key == "01x01-01x05":
            season, episode = int(groups[0]), int(groups[1])
            break
        elif key in ("Episode 01", "Ep01", "E01",
                     "Anime 01v2", "Episode 01v2",
                     "Part 01", "Chapter 01", "Volume",
                     "Mini Series Part", "WEB Episode",
                     "EP 01 END", "Disc 1", "CD1"):
            if not season:
                season = 0
            episode = int(groups[0])
            break
        elif key in ("Season 01", "S01", "Complete Season",
                     "Season Pack", "Batch"):
            season = int(groups[0])
            break
        elif key in ("OVA", "ONA", "Special"):
            ova_num = groups[0] if groups[0] else None
            if ova_num:
                episode = int(ova_num)
            break
        elif key in ("2024.01.15", "2024-01-15"):
            # date-based; ignore
            pass
        elif key == "Season Roman":
            roman = groups[0].lower()
            season = ROMAN_TO_INT.get(roman, 0)
            break
        elif key == "Episodes 1-10":
            episode = int(groups[0])
            break
        elif key in ("101", "1001", "0101"):
            digits = groups[0] + groups[1]
            if len(digits) == 3:
                season = int(digits[0])
                episode = int(digits[1:])
            else:
                season = int(digits[:2])
                episode = int(digits[2:])
            break

    return season, episode


def _is_series_file(file_name: str, caption: str = "") -> bool:
    """A file is a series file only if a season or episode number is detected."""
    s, e = _parse_season_episode(file_name)
    if s > 0 or e > 0:
        return True
    if caption:
        s2, e2 = _parse_season_episode(caption)
        if s2 > 0 or e2 > 0:
            return True
    return False


def _parse_quality(name: str, caption: str = "") -> str:
    combined = f"{name} {caption}"
    for pattern, label in _QUALITY_MAP:
        if pattern.search(combined):
            return label
    return "Unknown"


def _quality_from_resolution(width: int, height: int) -> str:
    long_side = max(width or 0, height or 0)
    if long_side >= 2160: return "4K"
    if long_side >= 1080: return "1080P"
    if long_side >= 720:  return "720P"
    if long_side >= 480:  return "480P"
    if long_side > 0:     return "360P"
    return "Unknown"


def _parse_codec(name: str, caption: str = "") -> str:
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


# ══════════════════════════════════════════════════════════════
# TMDB / OMDB LANGUAGE FALLBACK
# ══════════════════════════════════════════════════════════════

_LANG_CACHE: dict[str, str] = {}
_LANG_API_SEM = asyncio.Semaphore(3)


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

    acquired = _LANG_API_SEM.locked()
    if acquired and _CAPTION_LANG_CACHE is not None:
        _LANG_CACHE[key] = "Unknown"
        return "Unknown"

    try:
        async with _LANG_API_SEM:
            from CyberTG.poster import fetch_tmdb, fetch_omdb
            from utils.cyber_poster import save_cache

            tmdb = await fetch_tmdb(key)
            if tmdb:
                poster, year, genre, rating, lang = tmdb
                if lang and lang not in ("Original Language", "Unknown", ""):
                    await asyncio.to_thread(save_cache, key, poster, year, genre, rating, "tmdb", lang)
                    _LANG_CACHE[key] = lang
                    return lang

            omdb = await fetch_omdb(key)
            if omdb:
                poster, year, genre, rating, lang = omdb
                if lang and lang not in ("Original Language", "Unknown", ""):
                    await asyncio.to_thread(save_cache, key, poster, year, genre, rating, "omdb", lang)
                    _LANG_CACHE[key] = lang
                    return lang
    except Exception as e:
        logger.debug(f"[LANG] API lookup failed for {key!r}: {e}")

    _LANG_CACHE[key] = "Unknown"
    return "Unknown"


async def _resolve_language(file_name: str, caption: str, normalized_name: str) -> str:
    use_caption_lang = await _get_caption_lang_flag()
    if use_caption_lang and caption and caption.strip():
        lang = _parse_language(caption, "")
        if lang != "Unknown":
            return lang
    lang = _parse_language(file_name, "")
    if lang != "Unknown":
        return lang
    return await _get_language_from_api(normalized_name)


# ══════════════════════════════════════════════════════════════
# SERIES NAME NORMALIZER
# ══════════════════════════════════════════════════════════════

def _normalized_series_name(file_name: str) -> str:
    if not file_name:
        return ""
    name = file_name
    name = re.sub(r"\.(mkv|mp4|avi|mov|wmv|flv|webm)$", "", name, flags=re.IGNORECASE)
    name = _MENTION_RE.sub(" ", name)

    cut_match = None
    for key in [
        "S01E01E02", "S01E01-E05", "01x01-01x05",
        "S01E01", "S01-E01", "S01.E01", "S01_E01", "S01 E01",
        "S01 EP01", "Season Episode", "Season Ep", "01x01",
        "Episode 01", "Ep01", "E01",
        "Season 01", "S01", "Complete Season", "Season Pack", "Batch",
        "Anime 01v2", "S01E01v2", "Episode 01v2",
        "OVA", "ONA", "Special",
        "Part 01", "Chapter 01", "Volume",
        "2024.01.15", "2024-01-15", "Mini Series Part",
        "Season Roman", "WEB Episode", "EP 01 END", "Disc 1", "CD1",
        "Episodes 1-10", "101", "1001", "0101"
    ]:
        pat = PATTERNS.get(key)
        if pat:
            m = pat.search(name)
            if m:
                cut_match = m
                break

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
    while words and re.fullmatch(r"[A-Z]{1,4}", words[0]):
        words.pop(0)
    base = " ".join(words)
    base = re.sub(r"\b[a-zA-Z]{1,2}$", "", base).strip()
    parts = base.split()
    if len(parts) > 4:
        parts = parts[:4]
    return " ".join(parts).title()


# ══════════════════════════════════════════════════════════════
# MEDIA TYPE DETECTOR
# ══════════════════════════════════════════════════════════════

def _get_media_info(msg):
    if msg.video:
        return msg.video, "video"
    if msg.audio:
        return msg.audio, "audio"
    if msg.document:
        return msg.document, "document"
    return None, None


# ══════════════════════════════════════════════════════════════
# CODEC-AWARE DUPLICATE CHECK (single-doc — RTI only)
# ══════════════════════════════════════════════════════════════

async def _episode_exists_with_codec_check(
    normalized_name: str, season: int, episode: int,
    quality: str, language: str, new_codec: str,
) -> bool:
    from DB.sqlite_db import sql_db_lock as _sql_lock

    exists = await episode_exists(normalized_name, season, episode, quality, language)
    if not exists:
        return False
    if not new_codec:
        return True

    from DB.sqlite_db import _get_conn
    import asyncio as _asyncio

    def _check_existing_codec():
        conn   = _get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT codec FROM files
            WHERE normalized_name = ? AND season = ? AND episode = ? AND quality = ? AND language = ?
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
        return False
    return True


# ══════════════════════════════════════════════════════════════
# BATCH DEDUP CHECK
# ══════════════════════════════════════════════════════════════

async def batch_dedup_check(docs: list[dict]) -> tuple[list[dict], int]:
    if not docs:
        return [], 0

    seen: dict[tuple, dict] = {}
    for doc in docs:
        key = (doc["normalized_name"], doc["season"], doc["episode"], doc["quality"], doc["language"])
        seen[key] = doc
    deduped = list(seen.values())
    if not deduped:
        return [], len(docs)

    from DB.sqlite_db import _get_conn, sql_db_lock

    full_keys = [
        (d["normalized_name"], d["season"], d["episode"], d["quality"], d["language"])
        for d in deduped
    ]
    lang_agnostic_keys = [
        (d["normalized_name"], d["season"], d["episode"], d["quality"])
        for d in deduped
    ]

    def _bulk_exists_check() -> tuple[dict, set]:
        conn   = _get_conn()
        cursor = conn.cursor()
        CHUNK  = 190

        full_map: dict[tuple, str] = {}
        lang_agnostic_set: set[tuple] = set()

        # Exact match
        for i in range(0, len(full_keys), CHUNK):
            chunk        = full_keys[i : i + CHUNK]
            placeholders = ",".join(["(?,?,?,?,?)"] * len(chunk))
            flat_params  = [v for k in chunk for v in k]
            cursor.execute(f"""
                SELECT normalized_name, season, episode, quality, language, codec
                FROM files
                WHERE (normalized_name, season, episode, quality, language)
                      IN ({placeholders})
            """, flat_params)
            for row in cursor.fetchall():
                k = (row[0], row[1], row[2], row[3], row[4])
                full_map[k] = row[5] or ""

        # Language-agnostic
        remaining = [
            la_key for la_key, full_key in zip(lang_agnostic_keys, full_keys)
            if full_key not in full_map
        ]
        for i in range(0, len(remaining), CHUNK):
            chunk        = remaining[i : i + CHUNK]
            placeholders = ",".join(["(?,?,?,?)"] * len(chunk))
            flat_params  = [v for k in chunk for v in k]
            cursor.execute(f"""
                SELECT normalized_name, season, episode, quality
                FROM files
                WHERE (normalized_name, season, episode, quality)
                      IN ({placeholders})
                LIMIT {len(chunk) * 10}
            """, flat_params)
            for row in cursor.fetchall():
                lang_agnostic_set.add((row[0], row[1], row[2], row[3]))

        return full_map, lang_agnostic_set

    async with sql_db_lock:
        full_map, lang_agnostic_set = await asyncio.to_thread(_bulk_exists_check)

    to_save = []
    n_dup   = 0

    for doc, la_key in zip(deduped, lang_agnostic_keys):
        full_key = (doc["normalized_name"], doc["season"], doc["episode"], doc["quality"], doc["language"])
        if full_key in full_map:
            existing_codec = full_map[full_key]
            new_codec = doc.get("codec") or ""
            if new_codec and not existing_codec:
                logger.debug(
                    f"[DEDUP] Codec backfill: {doc['normalized_name']!r} "
                    f"S{doc['season']:02d}E{doc['episode']:02d} {doc['quality']} "
                    f"[{doc['language']}] codec={new_codec!r}"
                )
                to_save.append(doc)
            else:
                n_dup += 1
        elif la_key in lang_agnostic_set:
            logger.debug(
                f"[DEDUP] Lang-agnostic dup: {doc['normalized_name']!r} "
                f"S{doc['season']:02d}E{doc['episode']:02d} {doc['quality']} "
                f"(stored language differs from parsed '{doc['language']}')"
            )
            n_dup += 1
        else:
            to_save.append(doc)

    in_batch_dups = len(docs) - len(deduped)
    return to_save, n_dup + in_batch_dups


# ══════════════════════════════════════════════════════════════
# MAIN PUBLIC FUNCTION (RTI — real-time index)
# ══════════════════════════════════════════════════════════════

async def save_series_file_for_bulk_index(msg, target: str = "both") -> tuple[bool, int]:
    try:
        file_obj, media_type = _get_media_info(msg)
        if not file_obj:
            return False, 0

        file_name = getattr(file_obj, "file_name", None)
        if not file_name or not file_name.strip():
            return False, 0

        # Skip subtitle files (.srt, .vtt, .sub, .ass) and non-video documents
        if file_name.lower().endswith(('.srt', '.vtt', '.sub', '.ass', '.txt', '.zip', '.rar', '.pdf', '.jpg', '.png', '.jpeg', '.nfo', '.html', '.htm', '.url')):
            logger.debug(f"[cyber_index] ⏭ Subtitle / non-video file skipped: {file_name}")
            return False, 0

        caption_raw = getattr(msg, "caption", None) or ""
        if not isinstance(caption_raw, str):
            caption_raw = str(caption_raw)
        if not _is_series_file(file_name, caption_raw):
            logger.debug(f"[cyber_index] ⏭ Not a series file: {file_name}")
            return False, 0

        channel_id = msg.chat.id if msg.chat else None
        message_id = msg.id
        if not channel_id:
            return False, 0

        caption = getattr(msg, "caption", None) or ""
        if not isinstance(caption, str):
            caption = str(caption)

        season, episode = _parse_season_episode(file_name)
        quality = _parse_quality(file_name, caption)
        codec   = _parse_codec(file_name, caption)

        normalized_name = _normalized_series_name(file_name)
        if not normalized_name:
            logger.debug(f"[cyber_index] Skipped (no series name): {file_name}")
            return False, 0

        if quality == "Unknown" and media_type == "video":
            width  = getattr(file_obj, "width",  0) or 0
            height = getattr(file_obj, "height", 0) or 0
            if width or height:
                quality = _quality_from_resolution(width, height)

        language = await _resolve_language(file_name, caption, normalized_name)

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

        clean_kw = await _get_clean_keywords()
        file_doc = {
            "channel_id":      channel_id,
            "message_id":      message_id,
            "file_name":       file_name,
            "file_name_clean": _apply_clean(file_name, clean_kw),
            "normalized_name": norm_lower,
            "season":          season,
            "episode":         episode,
            "quality":         quality,
            "language":        language,
            "file_id":         file_obj.file_id,
            "media_type":      media_type,
            "caption":         caption,
            "caption_clean":   _apply_clean(caption, clean_kw),
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


# ══════════════════════════════════════════════════════════════
# BATCH API
# ══════════════════════════════════════════════════════════════

async def parse_message_to_doc(msg) -> dict | None:
    try:
        file_obj, media_type = _get_media_info(msg)
        if not file_obj:
            return None
        file_name = getattr(file_obj, "file_name", None)
        if not file_name or not file_name.strip():
            return None

        # Skip subtitle files (.srt, .vtt, .sub, .ass) and non-video documents
        if file_name.lower().endswith(('.srt', '.vtt', '.sub', '.ass', '.txt', '.zip', '.rar', '.pdf', '.jpg', '.png', '.jpeg', '.nfo', '.html', '.htm', '.url')):
            return None

        caption_raw = getattr(msg, "caption", None) or ""
        if not isinstance(caption_raw, str):
            caption_raw = str(caption_raw)
        if not _is_series_file(file_name, caption_raw):
            return None

        channel_id = msg.chat.id if msg.chat else None
        message_id = msg.id
        if not channel_id:
            return None

        caption = getattr(msg, "caption", None) or ""
        if not isinstance(caption, str):
            caption = str(caption)

        season, episode = _parse_season_episode(file_name)
        quality = _parse_quality(file_name, caption)
        codec   = _parse_codec(file_name, caption)

        if quality == "Unknown" and media_type == "video":
            width  = getattr(file_obj, "width",  0) or 0
            height = getattr(file_obj, "height", 0) or 0
            if width or height:
                quality = _quality_from_resolution(width, height)

        normalized_name = _normalized_series_name(file_name)
        if not normalized_name:
            return None

        language = await _resolve_language(file_name, caption, normalized_name)

        clean_kw = await _get_clean_keywords()
        return {
            "channel_id":      channel_id,
            "message_id":      message_id,
            "file_name":       file_name,
            "file_name_clean": _apply_clean(file_name, clean_kw),
            "normalized_name": normalized_name.lower(),
            "season":          season,
            "episode":         episode,
            "quality":         quality,
            "language":        language,
            "file_id":         file_obj.file_id,
            "media_type":      media_type,
            "caption":         caption,
            "caption_clean":   _apply_clean(caption, clean_kw),
            "codec":           codec,
        }
    except Exception as e:
        logger.error(f"[cyber_index] parse_message_to_doc error: {e}")
        return None


async def batch_save_file_docs(docs: list[dict], target: str = "both") -> int:
    from DB.database import save_file_docs_batch
    if not docs:
        return 0
    seen_keys: dict[tuple, dict] = {}
    for doc in docs:
        key = (doc["normalized_name"], doc["season"], doc["episode"], doc["quality"], doc["language"])
        seen_keys[key] = doc
    to_save = list(seen_keys.values())
    if not to_save:
        return 0
    saved = await save_file_docs_batch(to_save, target=target)
    return saved


# ══════════════════════════════════════════════════════════════
# STANDALONE PARSE HELPER
# ══════════════════════════════════════════════════════════════

def parse_file_name(file_name: str, caption: str = "") -> dict:
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
