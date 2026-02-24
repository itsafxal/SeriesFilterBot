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


# ── Junk-word cleanup ─────────────────────────────────────────

_CLEAN_TAGS = [
    "4K","1080P","720P","HDRIP","WEB-DL","WEBRIP","BLURAY","HDTV",
    "AMZN","NETFLIX","HULU","DDP5.1","X265","X264","10BIT","HEVC",
    "MKV","AAC","PSA","REPACK","COMPLETE","WEB",
]

_JUNK_WORDS = r"""
\b(
collection|pack|set|series|season|episode|ep|vol|volume|
batch|complete|full|release|mux|rip|hq|ultra|
theatre|theater|bluray|remux|webdl|webrip|web|
netflix|prime|amzn|hulu|hotstar|disney|hbo|
tgx|yts|subs|dub|audio|team|group|
4k|1080p|720p|480p|360p|
x264|x265|hevc|hdrip|aac|ddp5\.1|
rz
)\b
"""


def _clean_series_name_local(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r"[._\-]+", " ", name)
    name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", name)
    name = re.sub(r"[sS]\d+\s*[eE]\d+", " ", name)
    name = re.sub(_JUNK_WORDS, " ", name, flags=re.IGNORECASE | re.VERBOSE)
    name = re.sub(r"\b(19|20)\d{2}\b", " ", name)
    name = re.sub(r"[^a-zA-Z0-9\s]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\b[a-zA-Z]{1,2}\b$", "", name).strip()
    return name.title()


def _normalized_series_from_filename(file_name: str) -> str:
    if not file_name:
        return ""
    m = re.search(r"[sS](\d+)[._\-\s]*[eE](\d+)", file_name)
    base = file_name[:m.start()] if m else file_name
    base = _clean_series_name_local(base)
    parts = base.split()
    if len(parts) > 4:
        base = " ".join(parts[:4])
    return base.strip()


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
            UNIQUE(channel_id, message_id),
            UNIQUE(normalized_name, season, episode, quality, language)
        )
    """)

    # FIX: add media_type column if upgrading from old schema
    cursor.execute("PRAGMA table_info(files)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "media_type" not in existing_cols:
        cursor.execute("ALTER TABLE files ADD COLUMN media_type TEXT DEFAULT 'document'")

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
        "quality": 1, "language": 1, "file_id": 1, "media_type": 1, "_id": 0
    })

    batch = []
    total = 0
    last_log = 0

    for doc in mongo_docs:
        fn         = doc.get("file_name", "") or ""
        normalized = _normalized_series_from_filename(fn)
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
        ))

        if len(batch) >= BATCH_SIZE:
            cursor.executemany("""
                INSERT OR IGNORE INTO files
                (channel_id, message_id, file_name, normalized_name,
                 season, episode, quality, language, file_id, media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
             season, episode, quality, language, file_id, media_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    rows      = cursor.fetchall()
    languages = set()
    seasons   = []
    qualities = set()
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

    search_words       = q.split()
    full_phrase_pattern = "%" + "%".join(search_words) + "%"
    cursor.execute("""
        SELECT DISTINCT normalized_name FROM files
        WHERE normalized_name LIKE ? COLLATE NOCASE
    """, (full_phrase_pattern,))
    for r in cursor.fetchall():
        unique_results.add(r[0])

    final = list(unique_results)

    def sort_key(name):
        n = name.lower()
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


def get_file_by_message_id_and_channel_id_sync(channel_id: int, message_id: int):
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM files WHERE channel_id = ? AND message_id = ?
    """, (channel_id, message_id))
    row = cursor.fetchone()
    return dict(row) if row else None


def save_file_to_sql_sync(file_doc: dict, logger) -> bool:
    try:
        conn   = _get_conn()
        cursor = conn.cursor()

        normalized = (file_doc.get("normalized_name") or "").strip().lower()
        file_doc["normalized_name"] = normalized

        cursor.execute("""
            INSERT OR IGNORE INTO files
            (channel_id, message_id, file_name, normalized_name,
             season, episode, quality, language, file_id, media_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ))

        inserted = cursor.rowcount
        conn.commit()

        if inserted:
            clear_all_caches()

        return bool(inserted)

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
    conn.execute("DELETE FROM files WHERE channel_id = ? AND message_id = ?",
                 (channel_id, msg_id))
    conn.commit()
    clear_all_caches()


def get_total_files_sync():
    conn   = _get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM files")
    return cursor.fetchone()[0]
