"""
utils/cyber_index.py — ULTRA-FAST PARALLEL EDITION
====================================================
SPEED IMPROVEMENTS vs previous version:

  1. CAPTION_LANG CACHED — set_caption_lang_cache() stores the flag once
     at index start. Eliminates one MongoDB round-trip per document
     (was the #1 bottleneck — 200+ DB calls per 200-msg batch!).

  2. batch_dedup_check() — replaces N individual _episode_exists_with_codec_check()
     calls with a SINGLE SQLite IN() query for the whole batch.
     Example: 200 docs → was 200 SQL queries → now 1 query.

  3. parse_message_to_doc() uses cached CAPTION_LANG — zero DB calls during parse.

  4. _get_language_from_api() unchanged — still cached in _LANG_CACHE dict
     (in-memory, zero network calls for seen names).

All existing public functions (save_series_file_for_bulk_index, batch_save_file_docs,
parse_file_name) are fully backward-compatible.
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

# Codec: word-boundary guards prevent false positives (e.g. AVC inside INAVCODED)
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
# CAPTION_LANG CACHE  ← NEW
# Eliminates MongoDB round-trip on every parse_message_to_doc() call.
# index.py calls set_caption_lang_cache(True/False) once at index start,
# and set_caption_lang_cache(None) when done to revert to live DB lookups.
# ─────────────────────────────────────────────────────────────

_CAPTION_LANG_CACHE: bool | None = None   # None = not cached, use DB


def set_caption_lang_cache(value: bool | None):
    """
    Call with True/False to lock the CAPTION_LANG flag for the duration
    of a bulk index run (zero DB calls per doc).
    Call with None to clear the cache and revert to live DB reads.
    """
    global _CAPTION_LANG_CACHE
    _CAPTION_LANG_CACHE = value


async def _get_caption_lang_flag() -> bool:
    """
    Returns CAPTION_LANG setting.
    Uses cached value if set (fast path during bulk index).
    Falls back to MongoDB only when cache is None (RTI / normal ops).
    """
    if _CAPTION_LANG_CACHE is not None:
        return _CAPTION_LANG_CACHE
    flag = await get_global_setting("CAPTION_LANG", True)
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
    """
    Used by save_series_file_for_bulk_index (RTI path only).
    Batch indexing uses batch_dedup_check() instead — much faster.
    """
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
        logger.debug(
            f"[cyber_index] Codec backfill: {normalized_name!r} "
            f"S{season:02d}E{episode:02d} {quality} [{language}] "
            f"existing_codec='' new_codec={new_codec!r} → allowing upsert"
        )
        return False

    return True


# ─────────────────────────────────────────────────────────────
# BATCH DEDUP CHECK  ← NEW (replaces N individual dup checks)
# ─────────────────────────────────────────────────────────────

async def batch_dedup_check(docs: list[dict]) -> tuple[list[dict], int]:
    """
    Check an entire batch of docs for duplicates using a SINGLE SQL IN() query.

    Algorithm:
      1. Deduplicate within the batch itself (same content-identity → keep last).
      2. Build a SET of (normalized_name, season, episode, quality, language) keys
         that already exist in SQLite.
      3. A doc is a "save" candidate if:
           - It's not in the existing-set  (new doc), OR
           - It IS in the existing-set BUT has a codec while the existing row doesn't
             (codec backfill — the upsert will fill in the missing codec).
      4. Return (to_save_list, n_duplicates).

    This replaces up to SAVE_BATCH_SIZE individual episode_exists() calls
    with a single multi-row IN() query — massively faster for large batches.
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
        seen[key] = doc   # last wins
    deduped = list(seen.values())

    if not deduped:
        return [], len(docs) - 0

    # ── Step 2: bulk existence check via single SQL query ──────────────
    from DB.sqlite_db import _get_conn, sql_db_lock

    # Build the identity keys list
    identity_keys = [
        (d["normalized_name"], d["season"], d["episode"], d["quality"], d["language"])
        for d in deduped
    ]

    def _bulk_exists_check() -> dict[tuple, str]:
        """
        Returns a dict: identity_key → existing_codec ('' if no codec).
        Keys NOT present in the result do not exist in DB yet.
        """
        conn   = _get_conn()
        cursor = conn.cursor()

        # SQLite has a 999 variable limit. Each row uses 5 vars → max ~199 rows per query.
        # We chunk at 190 rows to be safe.
        CHUNK = 190
        result: dict[tuple, str] = {}

        for i in range(0, len(identity_keys), CHUNK):
            chunk = identity_keys[i : i + CHUNK]
            placeholders = ",".join(["(?,?,?,?,?)"] * len(chunk))
            flat_params  = []
            for k in chunk:
                flat_params.extend(k)

            cursor.execute(f"""
                SELECT normalized_name, season, episode, quality, language, codec
                FROM files
                WHERE (normalized_name, season, episode, quality, language)
                      IN ({placeholders})
            """, flat_params)

            for row in cursor.fetchall():
                key = (row[0], row[1], row[2], row[3], row[4])
                result[key] = row[5] or ""

        return result

    async with sql_db_lock:
        existing_map = await asyncio.to_thread(_bulk_exists_check)

    # ── Step 3: classify each deduped doc ─────────────────────────────
    to_save    = []
    n_dup      = 0

    for doc in deduped:
        key = (
            doc["normalized_name"],
            doc["season"],
            doc["episode"],
            doc["quality"],
            doc["language"],
        )

        if key not in existing_map:
            # Brand new doc — save it
            to_save.append(doc)
        else:
            existing_codec = existing_map[key]
            new_codec      = doc.get("codec") or ""

            if new_codec and not existing_codec:
                # Codec backfill — existing row lacks codec, new file has one
                logger.debug(
                    f"[DEDUP] Codec backfill allowed: {doc['normalized_name']!r} "
                    f"S{doc['season']:02d}E{doc['episode']:02d} {doc['quality']} "
                    f"[{doc['language']}] new_codec={new_codec!r}"
                )
                to_save.append(doc)
            else:
                # Genuine duplicate — skip
                n_dup += 1

    # Total duplicates = in-batch dups + DB dups
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

        # Use cached flag (fast) or live DB read (RTI)
        use_caption_lang = await _get_caption_lang_flag()
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

        season, episode = _parse_season_episode(file_name)
        quality         = _parse_quality(file_name, caption)
        codec           = _parse_codec(file_name, caption)

        # Resolution fallback for quality (video metadata)
        if quality == "Unknown" and media_type == "video":
            width  = getattr(file_obj, "width",  0) or 0
            height = getattr(file_obj, "height", 0) or 0
            if width or height:
                quality = _quality_from_resolution(width, height)

        # FAST PATH: use cached flag — zero DB calls
        use_caption_lang = await _get_caption_lang_flag()
        lang_caption     = caption if use_caption_lang else ""
        language         = _parse_language(file_name, lang_caption)
        normalized_name  = _normalized_series_name(file_name)

        if not normalized_name:
            return None

        # Language API fallback (in-memory _LANG_CACHE, rarely hits network)
        if language == "Unknown":
            language = await _get_language_from_api(normalized_name)

        return {
            "channel_id":      channel_id,
            "message_id":      message_id,
            "file_name":       file_name,
            "normalized_name": normalized_name.lower(),
            "season":          season,
            "episode":         episode,
            "quality":         quality,
            "language":        language,
            "file_id":         file_obj.file_id,
            "media_type":      media_type,
            "caption":         caption,
            "codec":           codec,
        }

    except Exception as e:
        logger.error(f"[cyber_index] parse_message_to_doc error: {e}")
        return None


async def batch_save_file_docs(docs: list[dict], target: str = "both") -> int:
    """
    Bulk-save a list of already-parsed, already-deduped file_doc dicts.

    NOTE: index.py now calls batch_dedup_check() BEFORE calling this function,
    so docs passed here are already filtered. This function just does the bulk write.

    For backward compatibility (if called directly), it still deduplicates internally.
    """
    from DB.database import save_file_docs_batch

    if not docs:
        return 0

    # In-batch dedup (fast, pure Python — no DB calls)
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
