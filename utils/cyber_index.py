"""
utils/cyber_index.py — FIXED EDITION
======================================

FIXES IN THIS VERSION:

  1. FALSE DUPLICATE BUG FIXED — batch_dedup_check now checks existence
     using BOTH the full key (name+season+ep+quality+lang) AND a language-
     agnostic key (name+season+ep+quality). If the language-agnostic key
     matches, we treat it as a duplicate regardless of what language the
     parser detected this run.

     Root cause: _get_language_from_api() can return "Unknown" if the TMDB
     API is slow/down, while the same file was previously stored as "Hindi".
     The full key doesn't match → dedup misses it → second row inserted with
     language="Unknown" → users see duplicates in search results.

  2. API CALLS NO LONGER BLOCK BATCH PROCESSING — _get_language_from_api()
     is now capped by a semaphore (max 3 concurrent HTTP calls). During bulk
     index, if the semaphore would block, we return "Unknown" immediately
     and let the existing language (from the DB, via dedup logic) win.
     The TMDB cache still fills up over time so repeat lookups are instant.

  3. CAPTION_LANG CACHED — unchanged from previous version.
     set_caption_lang_cache() stores the flag once at index start.
     Eliminates one MongoDB round-trip per document.

  4. batch_dedup_check() — SINGLE SQLite IN() query for the whole batch
     PLUS language-agnostic secondary check. Returns correct duplicate count.

All existing public functions are fully backward-compatible.
"""

import re
import asyncio
from DB.database import save_file_to_db, episode_exists, get_global_setting, get_caption_clean_keywords
from config import logger


# ─────────────────────────────────────────────────────────────
# CAPTION CLEAN CACHE
# ─────────────────────────────────────────────────────────────

_CLEAN_KW_CACHE: list | None = None


def set_clean_keywords_cache(keywords: list | None):
    global _CLEAN_KW_CACHE
    _CLEAN_KW_CACHE = keywords


async def _get_clean_keywords() -> list:
    if _CLEAN_KW_CACHE is not None:
        return _CLEAN_KW_CACHE
    return await get_caption_clean_keywords()


def _apply_clean(text: str, keywords: list) -> str:
    """Remove keywords from text."""
    if not text or not keywords:
        return text
    for kw in keywords:
        if kw:
            text = text.replace(kw, "")
    return text.strip()


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
# CAPTION_LANG CACHE
# ─────────────────────────────────────────────────────────────

_CAPTION_LANG_CACHE: bool | None = None


def set_caption_lang_cache(value: bool | None):
    global _CAPTION_LANG_CACHE
    _CAPTION_LANG_CACHE = value


async def _get_caption_lang_flag() -> bool:
    if _CAPTION_LANG_CACHE is not None:
        return _CAPTION_LANG_CACHE
    flag = await get_global_setting("CAPTION_LANG", False)
    return bool(flag)


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


# ─────────────────────────────────────────────────────────────
# SERIES GUARD — accepts any file that looks like a TV series.
# Rejects only plain movies / music / random docs with no season marker.
#
# Accepted:
#   S01E01, S01EP01, S01E01E02, Season 1 Episode 1, 1x01  — episode files
#   S01, S1, Season 1, Season01                            — season-only packs
#   Show.S02.Complete.mkv, Season 3 Batch                  — full-season packs
#
# Rejected: bare filenames with zero season/episode markers
# ─────────────────────────────────────────────────────────────

_SERIES_GUARD_RE = re.compile(
    r"""
    (?:
        [sS]\d{1,3}[\s._\-]*[eE][pP]?\d{1,3}          # S01E01 / S01EP01 / S01 E01
      | [sS]\d{1,3}[eE]\d{1,3}[eE]\d{1,3}              # S01E01E02 (multi-ep)
      | Season[\s._\-]*\d+[\s._\-]*Episode[\s._\-]*\d+ # Season 1 Episode 1
      | \d{1,2}x\d{1,2}                                 # 1x01
      | [sS]\d{1,3}\b                                   # S01 / S1 alone (season pack)
      | Season[\s._\-]*\d+\b                            # Season 1 / Season01 alone
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_series_file(file_name: str, caption: str = "") -> bool:
    """
    Returns True if the file contains any season or episode marker —
    including season-only packs like S01, Season 2, Show.S02.Complete.mkv.
    Returns False only for files with NO season/episode indicator at all.
    """
    combined = f"{file_name} {caption}"
    return bool(_SERIES_GUARD_RE.search(combined))


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


# ─────────────────────────────────────────────────────────────
# TMDB / OMDB LANGUAGE FALLBACK
# FIX: semaphore limits concurrent HTTP calls to prevent blocking
# ─────────────────────────────────────────────────────────────

_LANG_CACHE: dict[str, str] = {}
_LANG_API_SEM = asyncio.Semaphore(3)   # max 3 concurrent HTTP calls


async def _resolve_language(file_name: str, caption: str, normalized_name: str) -> str:
    """
    Language resolution — strict priority chain:

    CAPTION_LANG ENABLED:
      1. Search caption alone → found → use it
      2. Search file_name alone → found → use it
      3. TMDB / OMDB → found → use it
      4. Return "Unknown"

    CAPTION_LANG DISABLED:
      1. Search file_name alone → found → use it
      2. TMDB / OMDB → found → use it
      3. Return "Unknown"
    """
    use_caption_lang = await _get_caption_lang_flag()

    if use_caption_lang:
        # Step 1 — caption only
        if caption and caption.strip():
            lang = _parse_language(caption, "")
            if lang != "Unknown":
                return lang

    # Step 2 — filename only
    lang = _parse_language(file_name, "")
    if lang != "Unknown":
        return lang

    # Step 3 — TMDB / OMDB
    return await _get_language_from_api(normalized_name)
    """
    Returns language from cache or TMDB/OMDB API.
    Semaphore prevents more than 3 concurrent HTTP calls.
    If called during bulk index and semaphore would block, returns "Unknown"
    immediately — the batch is not held up by a slow API call.
    """
    if not normalized_name:
        return "Unknown"

    key = normalized_name.lower().strip()

    if key in _LANG_CACHE:
        return _LANG_CACHE[key]

    # ── SQLite cache (instant) ────────────────────────────────
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

    # ── HTTP API (rate-limited) ───────────────────────────────
    # During bulk index, if all 3 slots are busy, skip and return "Unknown"
    # so the batch isn't held up for a single unknown-language file.
    acquired = _LANG_API_SEM.locked()
    if acquired and _CAPTION_LANG_CACHE is not None:
        # We're in a bulk index run and semaphore is at limit — skip API
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
# CODEC-AWARE DUPLICATE CHECK  (single-doc — used by RTI only)
# ─────────────────────────────────────────────────────────────

async def _episode_exists_with_codec_check(
    normalized_name: str,
    season: int,
    episode: int,
    quality: str,
    language: str,
    new_codec: str,
) -> bool:
    """Used by save_series_file_for_bulk_index (RTI path only)."""
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
        return False

    return True


# ─────────────────────────────────────────────────────────────
# BATCH DEDUP CHECK — FIXED
# ─────────────────────────────────────────────────────────────

async def batch_dedup_check(docs: list[dict]) -> tuple[list[dict], int]:
    """
    Check an entire batch of docs for duplicates using SQL IN() queries.

    FIX: Two-level dedup check:
      1. EXACT match: (normalized_name, season, episode, quality, language)
         → straightforward duplicate.
      2. LANGUAGE-AGNOSTIC match: (normalized_name, season, episode, quality)
         → same episode already stored with a DIFFERENT language label
         (e.g. stored as "Hindi" but now parsed as "Unknown" because TMDB was slow).
         Without this check, the same episode gets inserted again as a separate row
         with language="Unknown", causing duplicate results in search.

    Returns (to_save_list, n_duplicates).
    """
    if not docs:
        return [], 0

    # ── Step 1: in-batch dedup (keep last occurrence per identity key) ──
    seen: dict[tuple, dict] = {}
    for doc in docs:
        key = (
            doc["normalized_name"],
            doc["season"],
            doc["episode"],
            doc["quality"],
            doc["language"],
        )
        seen[key] = doc
    deduped = list(seen.values())

    if not deduped:
        return [], len(docs)

    # ── Step 2: bulk existence checks via SQLite ───────────────────────
    from DB.sqlite_db import _get_conn, sql_db_lock

    full_keys = [
        (d["normalized_name"], d["season"], d["episode"], d["quality"], d["language"])
        for d in deduped
    ]
    # Language-agnostic keys (4-tuple): catches same ep stored with diff language
    lang_agnostic_keys = [
        (d["normalized_name"], d["season"], d["episode"], d["quality"])
        for d in deduped
    ]

    def _bulk_exists_check() -> tuple[dict, set]:
        """
        Returns:
          full_map: {(name,season,ep,quality,lang) → existing_codec}  — exact matches
          lang_agnostic_set: {(name,season,ep,quality)}  — any-language matches
        """
        conn   = _get_conn()
        cursor = conn.cursor()
        CHUNK  = 190  # SQLite 999-var limit; 5 vars/row → max 199 rows

        full_map: dict[tuple, str] = {}
        lang_agnostic_set: set[tuple] = set()

        # ── Exact-match check ─────────────────────────────────
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

        # ── Language-agnostic check ───────────────────────────
        # Only query for docs NOT already found in exact match
        # (no need to re-check those).
        remaining_lang_agnostic = [
            la_key
            for la_key, full_key in zip(lang_agnostic_keys, full_keys)
            if full_key not in full_map
        ]

        for i in range(0, len(remaining_lang_agnostic), CHUNK):
            chunk        = remaining_lang_agnostic[i : i + CHUNK]
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

    # ── Step 3: classify each deduped doc ─────────────────────────────
    to_save = []
    n_dup   = 0

    for doc, la_key in zip(deduped, lang_agnostic_keys):
        full_key = (
            doc["normalized_name"],
            doc["season"],
            doc["episode"],
            doc["quality"],
            doc["language"],
        )

        if full_key in full_map:
            # Exact match — check codec backfill
            existing_codec = full_map[full_key]
            new_codec      = doc.get("codec") or ""
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
            # FIX: same episode already exists with a different language label.
            # This happens when TMDB returns "Unknown" on this run but stored
            # as "Hindi" previously. Do NOT insert a second row — mark as dup.
            logger.debug(
                f"[DEDUP] Lang-agnostic dup: {doc['normalized_name']!r} "
                f"S{doc['season']:02d}E{doc['episode']:02d} {doc['quality']} "
                f"(stored language differs from parsed '{doc['language']}')"
            )
            n_dup += 1

        else:
            # Brand new doc — save it
            to_save.append(doc)

    in_batch_dups = len(docs) - len(deduped)
    return to_save, n_dup + in_batch_dups


# ─────────────────────────────────────────────────────────────
# MAIN PUBLIC FUNCTION  (RTI — real-time index, single file)
# ─────────────────────────────────────────────────────────────

async def save_series_file_for_bulk_index(
    msg,
    target: str = "both",
) -> tuple[bool, int]:
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

        # ── SERIES GUARD — skip non-series files (movies, music, etc.) ──
        caption_raw = getattr(msg, "caption", None) or ""
        if not isinstance(caption_raw, str):
            caption_raw = str(caption_raw)
        if not _is_series_file(file_name, caption_raw):
            logger.debug(f"[cyber_index] ⏭ Not a series file (no S/E marker): {file_name}")
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

        normalized_name  = _normalized_series_name(file_name)

        if not normalized_name:
            logger.debug(f"[cyber_index] Skipped (no series name): {file_name}")
            return False, 0

        if quality == "Unknown" and media_type == "video":
            width  = getattr(file_obj, "width",  0) or 0
            height = getattr(file_obj, "height", 0) or 0
            if width or height:
                quality = _quality_from_resolution(width, height)

        use_caption_lang = await _get_caption_lang_flag()
        language         = await _resolve_language(file_name, caption, normalized_name)

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


# ─────────────────────────────────────────────────────────────
# BATCH API — used by index.py batch engine
# ─────────────────────────────────────────────────────────────

async def parse_message_to_doc(msg) -> dict | None:
    """
    Parse a single Pyrogram Message into a file_doc dict WITHOUT saving.
    Uses cached CAPTION_LANG (zero DB calls during bulk index).
    """
    try:
        file_obj, media_type = _get_media_info(msg)
        if not file_obj:
            return None

        file_name = getattr(file_obj, "file_name", None)
        if not file_name or not file_name.strip():
            return None

        file_name  = file_name.strip()
        channel_id = msg.chat.id if msg.chat else None
        message_id = msg.id

        if not channel_id:
            return None

        caption = getattr(msg, "caption", None) or ""
        if not isinstance(caption, str):
            caption = str(caption)

        # ── SERIES GUARD — skip non-series files (movies, music, etc.) ──
        if not _is_series_file(file_name, caption):
            logger.debug(f"[cyber_index] ⏭ Not a series file (no S/E marker): {file_name}")
            return None

        season, episode = _parse_season_episode(file_name)
        quality         = _parse_quality(file_name, caption)
        codec           = _parse_codec(file_name, caption)

        if quality == "Unknown" and media_type == "video":
            width  = getattr(file_obj, "width",  0) or 0
            height = getattr(file_obj, "height", 0) or 0
            if width or height:
                quality = _quality_from_resolution(width, height)

        normalized_name = _normalized_series_name(file_name)

        if not normalized_name:
            return None

        use_caption_lang = await _get_caption_lang_flag()
        language         = await _resolve_language(file_name, caption, normalized_name)

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
    """
    Bulk-save a list of already-parsed, already-deduped file_doc dicts.
    """
    from DB.database import save_file_docs_batch

    if not docs:
        return 0

    # In-batch dedup (fast, pure Python)
    seen_keys: dict[tuple, dict] = {}
    for doc in docs:
        key = (
            doc["normalized_name"],
            doc["season"],
            doc["episode"],
            doc["quality"],
            doc["language"],
        )
        seen_keys[key] = doc

    to_save = list(seen_keys.values())
    if not to_save:
        return 0

    saved = await save_file_docs_batch(to_save, target=target)
    return saved


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
