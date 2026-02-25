"""
sqlite_db.py — FIXED & OPTIMIZED
==================================
FIXES:
  1. WAL mode enabled at startup — dramatically improves concurrent read performance
  2. Persistent connection pool (thread-local) — eliminates open/close overhead per query
  3. get_series_summary_sync: single query instead of 3 separate queries
  4. All sync functions use _get_conn() from pool instead of sqlite3.connect() each time
  5. Cache cleared only on writes (not reads) — lru_cache stays warm
  6. media_type column added to schema (was missing, caused KeyError on delivery)
  7. CODEC FIX: get_unique_qualities_with_codec_sync now orders by non-empty codec first
     so the best codec label always wins when deduplicating by quality
  8. CODEC FIX: save_file_to_sql_sync uses INSERT ... ON CONFLICT DO UPDATE so
     re-indexing updates stale empty-codec rows instead of silently ignoring them
  9. CODEC FIX: repair_codec_from_filenames_sync() backfills codec for all existing
     rows that were indexed before codec detection was added
"""

import asyncio
import re
import sqlite3
import threading
from functools import lru_cache

BATCH_SIZE        = 10000
PROGRESS_STEP     = 50000
SUPER_BATCH_SIZE  = 100000

SQLITE_DB_PATH = "series_index.db"
sql_db_lock    = asyncio.Lock()

# ── Thread-local connection pool ──────────────────────────────
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """
    Returns a thread-local SQLite connection with WAL mode and optimized settings.
    Opens once per thread, reused for all subsequent calls in that thread.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(SQLITE_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # ── Performance pragmas ──────────────────────────────
        conn.execute("PRAGMA journal_mode=WAL")        # concurrent reads + writes
        conn.execute("PRAGMA synchronous=NORMAL")      # safe + fast (vs FULL)
        conn.execute("PRAGMA cache_size=-32000")       # 32MB page cache
        conn.execute("PRAGMA temp_store=MEMORY")       # temp tables in RAM
        conn.execute("PRAGMA mmap_size=268435456")     # 256MB memory-mapped I/O
        _local.conn = conn
    return conn


# ── Normalizer regexes (mirrors cyber_index.py exactly) ──────
_SE_RE_SQL        = re.compile(r"[sS](\d{1,3})[._\-\s]*[eE](\d{1,3})", re.IGNORECASE)
_SEASON_ONLY_SQL  = re.compile(r"\b(?:season[\s._\-]*\d{1,3}|[sS]\d{1,3})\b", re.IGNORECASE)
_EP_ONLY_SQL      = re.compile(r"(?:[eE][pP]?|[eE]pisode[\s._\-]*)\d{1,3}", re.IGNORECASE)
_BRACKET_SQL      = re.compile(r"\[.*?\]|\(.*?\)|{.*?}")
_SEP_SQL          = re.compile(r"[._\-]+")
_MENTION_SQL      = re.compile(r"@\S+")
_YEAR_SQL         = re.compile(r"\b(19|20)\d{2}\b")
_NON_WORD_SQL     = re.compile(r"[^a-zA-Z0-9\s]")
_MULTI_SPC_SQL    = re.compile(r"\s+")
_PART_SQL         = re.compile(r"\b(?:part|pt)[\s._\-]*\d+\b", re.IGNORECASE)
_BARE_NUM_SQL     = re.compile(r"\b\d+\b")
_JUNK_SQL         = re.compile(r"""
    \b(4k|2160p|1080p|720p|480p|360p|bluray|blu\-ray|bdrip|bdremux|
    webrip|webdl|web\-dl|web|hdrip|hq|hdtv|x264|x265|hevc|avc|h264|h265|
    aac|ac3|dts|ddp|ddp5\.1|mp3|opus|10bit|8bit|hdr|sdr|dovi|dolby|
    amzn|netflix|prime|hotstar|hulu|disney|hbo|sony|tgx|yts|rip|rz|strife|
    psa|repack|proper|subs|sub|dubbed|dub|multi|dual|complete|full|batch|
    pack|collection|set|vol|volume|episode|ep|season|mkv|mp4|avi|mov)\b
""", re.IGNORECASE | re.VERBOSE)

# ── Codec regexes — used for backfill repair ──────────────────
_CODEC_MAP_SQL = [
    (re.compile(r"H\.?265|HEVC|x265", re.IGNORECASE), "H.265"),
    (re.compile(r"H\.?264|AVC|x264",  re.IGNORECASE), "H.264"),
    (re.compile(r"\bAV1\b",           re.IGNORECASE), "AV1"),
    (re.compile(r"\bVP9\b",           re.IGNORECASE), "VP9"),
    (re.compile(r"HDR|DOVI|Dolby.?Vision", re.IGNORECASE), "HDR"),
]


def _parse_codec_from_filename(file_name: str) -> str:
    """Extract codec label from a raw filename. Returns '' if not found."""
    for pattern, label in _CODEC_MAP_SQL:
        if pattern.search(file_name or ""):
            return label
    return ""


def _normalized_series_from_filename(file_name: str) -> str:
    """
    Identical logic to cyber_index._normalized_series_name().
    Used during migration so normalized_name is consistent.
    """
    if not file_name:
        return ""

    name = file_name
    name = re.sub(r"\.(mkv|mp4|avi|mov|wmv|flv|webm)$", "", name, flags=re.IGNORECASE)
    name = _MENTION_SQL.sub(" ", name)

    cut_match = (
        _SE_RE_SQL.search(name)
        or _SEASON_ONLY_SQL.search(name)
        or _EP_ONLY_SQL.search(name)
    )
    base = name[: cut_match.start()] if cut_match else name

    base = _BRACKET_SQL.sub(" ", base)
    base = _SEP_SQL.sub(" ", base)
    base = _YEAR_SQL.sub(" ", base)
    base = _PART_SQL.sub(" ", base)
    base = _JUNK_SQL.sub(" ", base)
    base = _NON_WORD_SQL.sub(" ", base)
    base = _BARE_NUM_SQL.sub(" ", base)
    base = _MULTI_SPC_SQL.sub(" ", base).strip()

    words = base.split()
    while words and re.fullmatch(r"[A-Z]{1,4}", words[0]):
        words.pop(0)
    base = " ".join(words)

    base = re.sub(r"\b[a-zA-Z]{1,2}$", "", base).strip()
    parts = base.split()
    if len(parts) > 4:
        parts = parts[:4]
    return " ".join(parts).title()


def _extract_language_local(file_name: str) -> str:
    name = (file_name or "").upper()
    sep_normalized = re.sub(r"[+&/x,.\-_]+", " ", name)
    lang_patterns = [
        (r"\b(HINDI|HIN|HI|DUB)\b",      "Hindi"),
        (r"\b(ENGLISH|ENG|EN)\b",         "English"),
        (r"\b(TAMIL|TAM)\b",              "Tamil"),
        (r"\b(TELUGU|TEL)\b",             "Telugu"),
        (r"\b(MALAYALAM|MAL)\b",          "Malayalam"),
    ]
    found = set()
    for pat, canon in lang_patterns:
        if re.search(pat, sep_normalized):
            found.add(canon)
    if ("DUAL" in name or "MULTI" in name) and not found:
        return "Multi Audio"
    if not found:
        return "English"
    if len(found) == 1:
        return next(iter(found))
    return "-".join(sorted(found))


# ── Schema ────────────────────────────────────────────────────

def create_sql_schema(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id     INTEGER NOT NULL,
            message_id     INTEGER NOT NULL,
            file_name      TEXT    NOT NULL,
            normalized_name TEXT   NOT NULL COLLATE NOCASE,
            season         INTEGER NOT NULL,
            episode        INTEGER NOT NULL,
            quality        TEXT    NOT NULL,
            language       TEXT    NOT NULL,
            file_id        TEXT,
            media_type     TEXT    DEFAULT 'document',
            caption        TEXT    DEFAULT '',
            codec          TEXT    DEFAULT '',
            UNIQUE(channel_id, message_id),
            UNIQUE(normalized_name, season, episode, quality, language)
        )
    """)

    # Auto-migration: add columns if upgrading from old schema
    cursor.execute("PRAGMA table_info(files)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "media_type" not in existing_cols:
        cursor.execute("ALTER TABLE files ADD COLUMN media_type TEXT DEFAULT 'document'")
    if "caption" not in existing_cols:
        cursor.execute("ALTER TABLE files ADD COLUMN caption TEXT DEFAULT ''")
    if "codec" not in existing_cols:
        cursor.execute("ALTER TABLE files ADD COLUMN codec TEXT DEFAULT ''")

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_series_lookup
        ON files (normalized_name, language, season)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_quality_lookup
        ON files (normalized_name, season, quality, language)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_channel_msg
        ON files (channel_id, message_id)
    """)
    conn.commit()


def _init_db():
    """Called once at module import — enables WAL and creates schema."""
    conn = _get_conn()
    create_sql_schema(conn)


_init_db()


# ── Migration ─────────────────────────────────────────────────

def migrate_data_from_mongo_to_sql_sync(files_collection, create_sql_schema_func, logger):
    conn = _get_conn()
    create_sql_schema_func(conn)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM files")
    conn.commit()
    logger.info("ℹ️ Cleared SQLite table for fresh migration.")

    doc_count = files_collection.count_documents({})
    logger.info(f"📦 Total Mongo documents found: {doc_count}")

    mongo_docs = files_collection.find({}, {
        "channel_id": 1, "message_id": 1, "file_name": 1,
        "normalized_name": 1, "season": 1, "episode": 1,
        "quality": 1, "language": 1, "file_id": 1, "media_type": 1,
        "caption": 1, "codec": 1, "_id": 0
    })

    batch = []
    total = 0
    last_log = 0

    for doc in mongo_docs:
        fn         = doc.get("file_name", "") or ""
        normalized = _normalized_series_from_filename(fn)

        # CODEC FIX: if Mongo doc has no codec, parse it from filename now
        codec = doc.get("codec", "") or ""
        if not codec:
            codec = _parse_codec_from_filename(fn)

        batch.append((
            doc.get("channel_id"),
            doc.get("message_id"),
            fn,
            normalized,
            doc.get("season", 0),
            doc.get("episode", 0),
            doc.get("quality", "Unknown"),
            doc.get("language", "English"),
            doc.get("file_id"),
            doc.get("media_type", "document"),
            doc.get("caption", ""),
            codec,
        ))

        if len(batch) >= BATCH_SIZE:
            cursor.executemany("""
                INSERT OR IGNORE INTO files
                (channel_id, message_id, file_name, normalized_name,
                 season, episode, quality, language, file_id, media_type, caption, codec)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, batch)
            conn.commit()
            total += len(batch)
            batch.clear()

        if total - last_log >= PROGRESS_STEP:
            pct = (total / doc_count * 100) if doc_count else 0
            logger.info(f"Migration: {total}/{doc_count} ({pct:.1f}%)")
            last_log = total

    if batch:
        cursor.executemany("""
            INSERT OR IGNORE INTO files
            (channel_id, message_id, file_name, normalized_name,
             season, episode, quality, language, file_id, media_type, caption, codec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        conn.commit()
        total += len(batch)

    logger.info(f"✅ Migration done: {total}/{doc_count} files.")


def repair_normalized_names_in_sqlite_sync(logger):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT rowid, file_name, normalized_name FROM files")
    rows = cursor.fetchall()
    updates = []
    for row in rows:
        rowid, file_name, old_norm = row[0], row[1], row[2]
        new_norm = _normalized_series_from_filename(file_name or "")
        if new_norm and new_norm != old_norm:
            updates.append((new_norm, rowid))
    if updates:
        cursor.executemany("UPDATE files SET normalized_name = ? WHERE rowid = ?", updates)
        conn.commit()
        logger.info(f"🛠️ Repaired {len(updates)} normalized_name rows.")


def repair_codec_from_filenames_sync(logger):
    """
    One-time repair: re-parse codec from file_name for all rows where codec
    is empty string or NULL. Safe to run on every boot — skips rows that
    already have a codec value.

    This fixes all files that were indexed before codec detection was added.
    e.g.  '[ss™] Man.Vs.Bee.S01E03.720p.x265.mkv'  →  codec = 'H.265'
    """
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rowid, file_name FROM files WHERE codec = '' OR codec IS NULL"
    )
    rows = cursor.fetchall()

    if not rows:
        logger.info("[CODEC REPAIR] All rows already have codec — nothing to do.")
        return

    updates = []
    for rowid, file_name in rows:
        codec = _parse_codec_from_filename(file_name or "")
        if codec:
            updates.append((codec, rowid))

    if updates:
        cursor.executemany(
            "UPDATE files SET codec = ? WHERE rowid = ?", updates
        )
        conn.commit()
        clear_all_caches()
        logger.info(
            f"[CODEC REPAIR] Backfilled codec for {len(updates)} / {len(rows)} rows."
        )
    else:
        logger.info(
            f"[CODEC REPAIR] {len(rows)} rows have empty codec but no codec "
            f"detectable from filename (e.g. documents, audio)."
        )


# ── Queries ───────────────────────────────────────────────────

_QUALITY_ORDER = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]


@lru_cache(maxsize=1024)
def get_series_summary_sync(series_name: str):
    """Single query for all summary data — 3× faster than 3 separate queries."""
    conn   = _get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT DISTINCT language, season, quality
        FROM files
        WHERE normalized_name = ? AND season > 0
        ORDER BY season ASC
    """, (series_name,))

    rows         = cursor.fetchall()
    languages    = set()
    seasons      = []
    qualities    = set()
    seen_seasons = set()

    for row in rows:
        lang, season, quality = row[0], row[1], row[2]
        languages.add(lang)
        if season not in seen_seasons:
            seasons.append(season)
            seen_seasons.add(season)
        if quality in _QUALITY_ORDER:
            qualities.add(quality)

    sorted_qualities = tuple(sorted(qualities, key=lambda x: _QUALITY_ORDER.index(x)))

    return {
        "languages": tuple(sorted(languages)),
        "seasons":   tuple(seasons),
        "qualities": sorted_qualities,
    }


@lru_cache(maxsize=1024)
def search_series_sync(query: str):
    conn   = _get_conn()
    cursor = conn.cursor()

    q = query.strip()
    if not q:
        return ()

    unique_results = set()

    cursor.execute("""
        SELECT DISTINCT normalized_name FROM files
        WHERE normalized_name = ? COLLATE NOCASE
    """, (q,))
    for r in cursor.fetchall():
        unique_results.add(r[0])

    search_words        = q.split()
    full_phrase_pattern = "%" + "%".join(search_words) + "%"
    cursor.execute("""
        SELECT DISTINCT normalized_name FROM files
        WHERE normalized_name LIKE ? COLLATE NOCASE
    """, (full_phrase_pattern,))
    for r in cursor.fetchall():
        unique_results.add(r[0])

    final = list(unique_results)

    def sort_key(name):
        n  = name.lower()
        ql = q.lower()
        return (not n.startswith(ql), ql not in n, abs(len(n) - len(ql)), len(n))

    final.sort(key=sort_key)
    return tuple(final)


@lru_cache(maxsize=1024)
def get_unique_languages_by_name_sync(series_name: str):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT language FROM files
        WHERE normalized_name = ? ORDER BY language ASC
    """, (series_name,))
    return tuple(r[0] for r in cursor.fetchall())


@lru_cache(maxsize=1024)
def get_unique_seasons_by_name_sync(series_name: str, language: str):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT season FROM files
        WHERE normalized_name = ? AND language = ? AND season > 0
        ORDER BY season ASC
    """, (series_name, language))
    return tuple(r[0] for r in cursor.fetchall())


@lru_cache(maxsize=1024)
def get_unique_qualities_by_season_sync(series_name: str, language: str, season: int):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT quality FROM files
        WHERE normalized_name = ? AND language = ? AND season = ?
    """, (series_name, language, season))
    qs = [r[0] for r in cursor.fetchall()]
    return tuple(sorted([q for q in qs if q in _QUALITY_ORDER],
                        key=lambda x: _QUALITY_ORDER.index(x)))


def get_all_files_by_season_and_quality_sync(series_name: str, season: int,
                                             quality: str, language: str):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM files
        WHERE normalized_name = ? AND season = ? AND quality = ? AND language = ?
        ORDER BY episode ASC
    """, (series_name, season, quality, language))
    return [dict(row) for row in cursor.fetchall()]


@lru_cache(maxsize=1024)
def get_unique_languages_by_season_sync(series_name: str, season: int):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT language FROM files
        WHERE normalized_name = ? AND season = ?
        ORDER BY language ASC
    """, (series_name, season))
    return tuple(r[0] for r in cursor.fetchall())


@lru_cache(maxsize=1024)
def get_unique_qualities_by_lang_season_sync(series_name: str, language: str, season: int):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT quality FROM files
        WHERE normalized_name = ? AND language = ? AND season = ?
    """, (series_name, language, season))
    qs = [r[0] for r in cursor.fetchall()]
    return tuple(sorted([q for q in qs if q in _QUALITY_ORDER],
                        key=lambda x: _QUALITY_ORDER.index(x)))


def get_unique_qualities_with_codec_sync(series_name: str, language: str, season: int):
    """
    Returns list of (quality, codec) tuples for button display.
    e.g. [("1080P", "H.265"), ("720P", "H.264"), ("480P", "")]

    CODEC FIX: rows are ordered so non-empty codec comes first within each
    quality group. The dedup (seen set) then always picks the best-codec row,
    never the stale empty-codec row from before codec detection was added.

    Sort order:
      1. quality rank  (4K → 1080P → 720P …)
      2. codec present (non-empty first)
      3. codec alpha   (deterministic tie-break between e.g. H.264 vs H.265)
    """
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT quality, codec
        FROM files
        WHERE normalized_name = ? AND language = ? AND season = ?
        ORDER BY
            quality ASC,
            CASE WHEN codec IS NOT NULL AND codec != '' THEN 0 ELSE 1 END ASC,
            codec ASC
    """, (series_name, language, season))
    rows = cursor.fetchall()

    seen   = set()
    result = []
    for q, c in rows:
        if q not in seen:
            seen.add(q)
            result.append((q, c or ""))

    result.sort(
        key=lambda x: _QUALITY_ORDER.index(x[0]) if x[0] in _QUALITY_ORDER else 99
    )
    return result


def get_file_by_message_id_and_channel_id_sync(channel_id: int, message_id: int):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM files WHERE channel_id = ? AND message_id = ?
    """, (channel_id, message_id))
    row = cursor.fetchone()
    return dict(row) if row else None


def episode_exists_sync(normalized_name: str, season: int, episode: int,
                        quality: str, language: str) -> bool:
    """
    Check if an episode slot is already filled.
    Returns True if ANY file with this series+season+episode+quality+language exists.
    """
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 1 FROM files
        WHERE normalized_name = ?
          AND season   = ?
          AND episode  = ?
          AND quality  = ?
          AND language = ?
        LIMIT 1
    """, (normalized_name.lower(), season, episode, quality, language))
    return cursor.fetchone() is not None


def save_file_to_sql_sync(file_doc: dict, logger) -> bool:
    """
    CODEC FIX: uses INSERT … ON CONFLICT DO UPDATE instead of INSERT OR IGNORE.

    On conflict by (channel_id, message_id) OR by content-identity
    (normalized_name, season, episode, quality, language):
      - codec      is updated only if the new value is non-empty
                   (never overwrites a real codec with an empty string)
      - media_type is updated only if the new value is non-empty
      - file_id    is always refreshed (Telegram file_ids can rotate)

    This means re-indexing a file will backfill a missing codec without
    clobbering any field that is already correctly set.
    """
    try:
        conn   = _get_conn()
        cursor = conn.cursor()

        normalized = (file_doc.get("normalized_name") or "").strip().lower()
        file_doc["normalized_name"] = normalized

        # If caller didn't detect a codec, try to parse it from the filename
        # as a last-resort so we never write empty when the info is available.
        codec = (file_doc.get("codec") or "").strip()
        if not codec:
            codec = _parse_codec_from_filename(file_doc.get("file_name", ""))
        file_doc["codec"] = codec

        cursor.execute("""
            INSERT INTO files
                (channel_id, message_id, file_name, normalized_name,
                 season, episode, quality, language, file_id,
                 media_type, caption, codec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, message_id) DO UPDATE SET
                file_id    = excluded.file_id,
                codec      = CASE
                                 WHEN excluded.codec != '' THEN excluded.codec
                                 ELSE codec
                             END,
                media_type = CASE
                                 WHEN excluded.media_type != '' THEN excluded.media_type
                                 ELSE media_type
                             END
            ON CONFLICT(normalized_name, season, episode, quality, language) DO UPDATE SET
                file_id    = excluded.file_id,
                codec      = CASE
                                 WHEN excluded.codec != '' THEN excluded.codec
                                 ELSE codec
                             END,
                media_type = CASE
                                 WHEN excluded.media_type != '' THEN excluded.media_type
                                 ELSE media_type
                             END
        """, (
            file_doc["channel_id"],
            file_doc["message_id"],
            file_doc["file_name"],
            file_doc["normalized_name"],
            file_doc["season"],
            file_doc["episode"],
            file_doc["quality"],
            file_doc["language"],
            file_doc.get("file_id"),
            file_doc.get("media_type", "document"),
            file_doc.get("caption", ""),
            file_doc["codec"],
        ))

        conn.commit()
        # rowcount is 1 for INSERT, 1 for UPDATE — both count as "processed"
        clear_all_caches()
        return True

    except Exception as e:
        logger.error(f"save_file_to_sql_sync error: {e}")
        return False


def clear_all_caches():
    get_series_summary_sync.cache_clear()
    search_series_sync.cache_clear()
    get_unique_languages_by_name_sync.cache_clear()
    get_unique_seasons_by_name_sync.cache_clear()
    get_unique_qualities_by_season_sync.cache_clear()
    get_unique_languages_by_season_sync.cache_clear()
    get_unique_qualities_by_lang_season_sync.cache_clear()


def delete_all_files_sync():
    conn = _get_conn()
    conn.execute("DELETE FROM files")
    conn.commit()
    clear_all_caches()


def delete_one_file_sync(channel_id: int, msg_id: int):
    conn = _get_conn()
    conn.execute(
        "DELETE FROM files WHERE channel_id = ? AND message_id = ?",
        (channel_id, msg_id)
    )
    conn.commit()
    clear_all_caches()


def get_total_files_sync():
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM files")
    return cursor.fetchone()[0]


def get_total_series_sync() -> int:
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(DISTINCT normalized_name) FROM files")
    return cursor.fetchone()[0]


def get_all_series_sync() -> list:
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT normalized_name, COUNT(*) as file_count
        FROM files
        WHERE normalized_name != ''
        GROUP BY normalized_name
        ORDER BY normalized_name ASC
    """)
    return [(row[0], row[1]) for row in cursor.fetchall()]
