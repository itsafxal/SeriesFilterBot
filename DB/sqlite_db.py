import asyncio
import re
import sqlite3
from functools import lru_cache  # <--- caching

# -----------------------------
# SQLITE SETUP
# -----------------------------
SQLITE_DB_PATH = "series_index.db"
sql_db_lock = asyncio.Lock()

# -----------------------------
# Levenshtein (Fuzzy distance)
# -----------------------------
def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        current = [i + 1]
        for j, cb in enumerate(b):
            insertions = previous[j + 1] + 1
            deletions = current[j] + 1
            substitutions = previous[j] + (ca != cb)
            current.append(min(insertions, deletions, substitutions))
        previous = current
    return previous[-1]

# -----------------------------
# CLEAN NAME HELPERS
# -----------------------------
_CLEAN_TAGS = [
    "4K", "1080P", "720P", "HDRIP", "WEB-DL", "WEBRIP", "BLURAY",
    "HDTV", "AMZN", "NETFLIX", "HULU", "DDP5.1", "X265", "X264",
    "10BIT", "HEVC", "MKV", "AAC", "PSA", "REPACK", "COMPLETE", "WEB"
]

def _clean_series_name_local(name: str) -> str:
    name = re.sub(r"[._\s]+", " ", name).strip()
    name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", name).strip()
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)

    for term in _CLEAN_TAGS + [r"(19|20)\d{2}"]:
        t = term
        if term != r"(19|20)\d{2}":
            t = rf"\b{re.escape(term)}\b"
        name = re.sub(t, " ", name, flags=re.IGNORECASE).strip()

    for tag in ["CK", "UB", "TG", "MOVIE HUB", "FILM"]:
        tag_regex = rf"(?:^|[\W_]+){re.escape(tag)}(?:$|[\W_]+)"
        name = re.sub(tag_regex, " ", name, flags=re.IGNORECASE).strip()

    name = re.sub(r"\s+", " ", name).strip()

    name = re.sub(
        r'^(?:collection|pack|set|series|season|episode|ep|vol|volume|batch|complete|full|合集|koleksi|release|mux|rip|hq|ultra|theatre|web|netflix|prime|amzn|hulu|hotstar|disney|hbo|yts|subs|dub)\b',
        ' ',
        name,
        flags=re.IGNORECASE
    )
    return name.title()

def _normalized_series_from_filename(file_name: str) -> str:
    if not file_name:
        return ""
    m = re.search(r"[sS](\d+)[._\-\s]*[eE](\d+)", file_name)
    if not m:
        return _clean_series_name_local(file_name)
    left = file_name[:m.start()]
    base = _clean_series_name_local(left) or _clean_series_name_local(file_name)
    parts = base.split()
    if len(parts) > 5:
        base = " ".join(parts[:5])
    return base.title()

def _extract_language_local(file_name: str) -> str:
    name = (file_name or "").upper()
    sep = re.sub(r"[+&/x,.\-_]+", " ", name)

    patterns = [
        (r"\b(HINDI|HIN|HI|DUB)\b", "Hindi"),
        (r"\b(ENGLISH|ENG|EN)\b", "English"),
        (r"\b(TAMIL|TAM)\b", "Tamil"),
        (r"\b(TELUGU|TEL)\b", "Telugu"),
        (r"\b(MALAYALAM|MAL)\b", "Malayalam")
    ]

    found = {canon for pat, canon in patterns if re.search(pat, sep)}

    if ("DUAL" in name or "MULTI" in name) and not found:
        return "Multi Audio"

    if not found:
        return "English"

    if len(found) == 1:
        return next(iter(found))

    return "-".join(sorted(found))

# -----------------------------
# CREATE SQLITE SCHEMA
# -----------------------------
def create_sql_schema(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            quality TEXT NOT NULL,
            language TEXT NOT NULL,
            file_id TEXT,
            UNIQUE(channel_id, message_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_norm_lang_season ON files (normalized_name, language, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_name_season_quality_lang ON files (normalized_name, season, quality, language)")
    conn.commit()

# -----------------------------
# MIGRATION (FROM MONGO)
# -----------------------------
def migrate_data_from_mongo_to_sql_sync(files_collection, create_schema, logger):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.create_function("levenshtein", 2, levenshtein)
    create_schema(conn)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM files")
    conn.commit()
    logger.info("SQLite table cleared.")

    mongo_docs = files_collection.find({}, {
        "channel_id": 1, "message_id": 1, "file_name": 1,
        "season": 1, "episode": 1, "quality": 1, "language": 1, "file_id": 1
    })

    for doc in mongo_docs:
        fn = doc.get("file_name", "")
        normalized = _normalized_series_from_filename(fn)
        language = _extract_language_local(fn)
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO files
                (channel_id, message_id, file_name, normalized_name, season, episode, quality, language, file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                doc["channel_id"], doc["message_id"], fn,
                normalized, doc.get("season", 0), doc.get("episode", 0),
                doc.get("quality", "Unknown"), language,
                doc.get("file_id")
            ))
        except Exception as e:
            logger.error(f"SQL insert error: {e}")

    conn.commit()
    conn.close()

# -----------------------------
# Cache clear
# -----------------------------
def clear_all_caches():
    search_series_sync.cache_clear()
    get_series_summary_sync.cache_clear()
    get_unique_languages_by_season_sync.cache_clear()
    get_unique_qualities_by_lang_season_sync.cache_clear()
    get_unique_languages_by_name_sync.cache_clear()
    get_unique_seasons_by_name_sync.cache_clear()
    get_unique_qualities_by_season_sync.cache_clear()

# -----------------------------
# SMART SEARCH (Elasticsearch-Style)
# -----------------------------
@lru_cache(maxsize=1024)
def search_series_sync(query: str):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.create_function("levenshtein", 2, levenshtein)
    cursor = conn.cursor()

    q = query.strip()
    if not q:
        conn.close()
        return tuple()

    scored = {}

    def add(names, score):
        for n in names:
            if n:
                scored[n] = scored.get(n, 0) + score

    # exact match
    cursor.execute("SELECT DISTINCT normalized_name FROM files WHERE normalized_name = ? COLLATE NOCASE", (q,))
    add([r[0] for r in cursor.fetchall()], 100)

    # startswith
    cursor.execute("SELECT DISTINCT normalized_name FROM files WHERE normalized_name LIKE ? COLLATE NOCASE", (q + "%",))
    add([r[0] for r in cursor.fetchall()], 80)

    # contains
    cursor.execute("SELECT DISTINCT normalized_name FROM files WHERE normalized_name LIKE ? COLLATE NOCASE", ("%" + q + "%",))
    add([r[0] for r in cursor.fetchall()], 60)

    # soundex
    cursor.execute("SELECT DISTINCT normalized_name FROM files WHERE soundex(normalized_name) = soundex(?)", (q,))
    add([r[0] for r in cursor.fetchall()], 40)

    # fuzzy levenshtein
    cursor.execute("SELECT DISTINCT normalized_name FROM files")
    all_names = [r[0] for r in cursor.fetchall()]

    for name in all_names:
        dist = levenshtein(name.lower(), q.lower())
        if dist <= 2:
            add([name], 30 - (dist * 10))  # 30,20,10

    conn.close()

    final = sorted(scored.items(), key=lambda x: x[1], reverse=True)

    return tuple([name for name, sc in final])

# -----------------------------
# SUMMARY + LANGUAGE/QUALITY FUNCTIONS
# -----------------------------
@lru_cache(maxsize=1024)
def get_series_summary_sync(series_name: str):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT DISTINCT language FROM files WHERE normalized_name = ?", (series_name,))
    langs = tuple([r[0] for r in cursor.fetchall()])

    cursor.execute("SELECT DISTINCT season FROM files WHERE normalized_name = ? ORDER BY season", (series_name,))
    seasons = tuple([r[0] for r in cursor.fetchall()])

    cursor.execute("SELECT DISTINCT quality FROM files WHERE normalized_name = ?", (series_name,))
    qualities = [r[0] for r in cursor.fetchall()]
    conn.close()

    order = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]
    qualities = tuple(sorted([q for q in qualities if q in order], key=lambda x: order.index(x)))

    return {
        "languages": langs,
        "seasons": seasons,
        "qualities": qualities
    }

@lru_cache(maxsize=1024)
def get_unique_languages_by_name_sync(series_name: str):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT language FROM files WHERE normalized_name = ?", (series_name,))
    langs = tuple([r[0] for r in cursor.fetchall()])
    conn.close()
    return langs

@lru_cache(maxsize=1024)
def get_unique_seasons_by_name_sync(series_name: str, language: str):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT season FROM files 
        WHERE normalized_name = ? AND language = ?
        ORDER BY season
    """, (series_name, language))
    seasons = tuple([r[0] for r in cursor.fetchall()])
    conn.close()
    return seasons

@lru_cache(maxsize=1024)
def get_unique_qualities_by_season_sync(series_name: str, language: str, season: int):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT quality FROM files
        WHERE normalized_name = ? AND language = ? AND season = ?
    """, (series_name, language, season))
    qualities = [r[0] for r in cursor.fetchall()]
    conn.close()

    order = ["4K", "1080P", "720P", "480P", "360P"]
    return tuple(sorted([q for q in qualities if q in order], key=lambda x: order.index(x)))

@lru_cache(maxsize=1024)
def get_unique_languages_by_season_sync(series_name: str, season: int):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT language FROM files
        WHERE normalized_name = ? AND season = ?
    """, (series_name, season))
    langs = tuple([r[0] for r in cursor.fetchall()])
    conn.close()
    return langs

@lru_cache(maxsize=1024)
def get_unique_qualities_by_lang_season_sync(series_name: str, language: str, season: int):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT quality FROM files
        WHERE normalized_name = ? AND language = ? AND season = ?
    """, (series_name, language, season))
    q = [r[0] for r in cursor.fetchall()]
    conn.close()

    order = ["4K", "1080P", "720P", "480P", "360P"]
    return tuple(sorted([x for x in q if x in order], key=lambda x: order.index(x)))

# -----------------------------
# GET FILE BY MESSAGE ID / CHANNEL
# -----------------------------
def get_file_by_message_id_and_channel_id_sync(channel_id: int, message_id: int):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM files WHERE channel_id = ? AND message_id = ?", (channel_id, message_id))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

# -----------------------------
# INSERT / UPDATE FILE
# -----------------------------
def save_file_to_sql_sync(file_doc: dict, logger):
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.create_function("levenshtein", 2, levenshtein)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO files
            (channel_id, message_id, file_name, normalized_name, season, episode, quality, language, file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            file_doc["channel_id"], file_doc["message_id"], file_doc["file_name"],
            file_doc["normalized_name"], file_doc["season"], file_doc["episode"],
            file_doc["quality"], file_doc["language"], file_doc.get("file_id")
        ))
        conn.commit()
        conn.close()
        clear_all_caches()
        return True
    except Exception as e:
        logger.error(f"SQLite save error: {e}")
        return False

# -----------------------------
# DELETE FUNCTIONS
# -----------------------------
def delete_all_files_sync():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM files")
    conn.commit()
    conn.close()
    clear_all_caches()

def delete_one_file_sync(channel_id: int, msg_id: int):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM files WHERE channel_id = ? AND message_id = ?", (channel_id, msg_id))
    conn.commit()
    conn.close()
    clear_all_caches()

def get_total_files_sync():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM files")
    count = cursor.fetchone()[0]
    conn.close()
    return count
