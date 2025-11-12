# sqlite_db.py
import asyncio
import re
import sqlite3

# --- SQLITE CONFIG AND STATE ---
# Note: logger and files_collection are passed from database.py context where needed
SQLITE_DB_PATH = "series_index.db"
sql_db_lock = asyncio.Lock() # Defined here but imported/used in database.py for async wrappers

# ---- Helpers to recompute normalized series names (Copied to avoid circular import)

_CLEAN_TAGS = [
    "4K","1080P","720P","HDRIP","WEB-DL","WEBRIP","BLURAY","HDTV",
    "AMZN","NETFLIX","HULU","DDP5.1","X265","X264","10BIT","HEVC",
    "MKV","AAC","PSA","REPACK","COMPLETE","WEB",
]

def _clean_series_name_local(name: str) -> str:
    name = re.sub(r"[._\s]+", " ", name).strip()
    name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", name).strip()
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    for term in _CLEAN_TAGS + [r"(19|20)\d{2}"]:
        t = term
        if term != r"(19|20)\d{2}":
            t = rf"\b{re.escape(term)}\b"
        name = re.sub(t, " ", name, flags=re.IGNORECASE).strip()
    # common channel suffixes
    for tag in ["CK","UB","TG","MOVIE HUB","FILM"]:
        tag_regex = rf"(?:^|[\W_]+){re.escape(tag)}(?:$|[\W_]+)"
        name = re.sub(tag_regex, " ", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(
    r'^(?:collection|pack|set|series|season|episode|ep|vol|volume|batch|complete|full|合集|koleksi|release|mux|rip|hq|ultra|theatre|theater|bluray|remux|webdl|webrip|web|netflix|prime|amzn|hulu|hotstar|disney|hbo|tgx|yts|subs|dub|audio|team|group)\b',
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
    sep_normalized = re.sub(r"[+&/x,.\-_]+", " ", name)

    lang_patterns = [
        (r"\b(HINDI|HIN|HI|DUB)\b", "Hindi"),
        (r"\b(ENGLISH|ENG|EN)\b", "English"),
        (r"\b(TAMIL|TAM)\b", "Tamil"),
        (r"\b(TELUGU|TEL)\b", "Telugu"),
        (r"\b(MALAYALAM|MAL)\b", "Malayalam"),
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


# --- SQLITE SCHEMA AND MIGRATION (SYNC) ---

def create_sql_schema(conn):
    """Creates the necessary table in the SQLite database, including 'language'."""
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
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_normalized_season_language ON files (normalized_name, language, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_name_season_quality_language ON files (normalized_name, season, quality, language)")
    conn.commit()

def migrate_data_from_mongo_to_sql_sync(files_collection, create_sql_schema_func, logger):
    """Synchronously migrates all data from MongoDB's 'files' collection to SQLite."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    create_sql_schema_func(conn)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM files")
    conn.commit()
    logger.info("ℹ️ Cleared SQLite table for fresh migration.")

    mongo_docs = files_collection.find({}, {
        "channel_id": 1, "message_id": 1, "file_name": 1, 
        "normalized_name": 1, "season": 1, "episode": 1, 
        "quality": 1, "language": 1, "file_id": 1, "_id": 0 # Ensure file_id is retrieved
    })
    
    unique = {}
    for doc in mongo_docs:
        fn = doc.get("file_name", "") or ""
        normalized = _normalized_series_from_filename(fn)
        
        season = doc.get("season", 0)
        episode = doc.get("episode", 0)
        quality = doc.get("quality", "Unknown")
        language = _extract_language_local(fn)

        key = (normalized, season, episode, quality, language)
        if key in unique:
            logger.info(f"[MIGRATION SKIP] Duplicate → {normalized} | S{season}E{episode} | {quality} | {language}")
            continue

        unique[key] = (
            doc.get("channel_id"),
            doc.get("message_id"),
            fn,
            normalized,
            season,
            episode,
            quality,
            language,
            doc.get("file_id") # <-- This is the 9th binding
        )

    insert_data = list(unique.values())

    if insert_data:
        try:
            # FIX: Updated statement to explicitly list 9 columns and 9 placeholders
            cursor.executemany("""
                INSERT OR IGNORE INTO files 
                (channel_id, message_id, file_name, normalized_name, season, episode, quality, language, file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, insert_data)
            conn.commit()
            logger.info(f"✅ Migrated {len(insert_data)} unique documents from MongoDB to SQLite.")
        except Exception as e:
            logger.error(f"❌ Failed to execute bulk SQL insert during migration: {e}")
    
    conn.close()
    
def repair_normalized_names_in_sqlite_sync(logger):
    """
    Synchronously recomputes normalized_name for all files in SQLite from the file_name.
    """
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT rowid, file_name, normalized_name FROM files")
    rows = cursor.fetchall()
    updates = []
    for rowid, file_name, old_norm in rows:
        new_norm = _normalized_series_from_filename(file_name or "")
        if new_norm and new_norm != old_norm:
            updates.append((new_norm, rowid))
    if updates:
        cursor.executemany("UPDATE files SET normalized_name = ? WHERE rowid = ?", updates)
        conn.commit()
        logger.info(f"🛠️ Repaired {len(updates)} normalized_name rows in SQLite.")
    conn.close()


# --- SEARCH AND RETRIEVAL FUNCTIONS (SYNC) ---

def get_series_summary_sync(series_name: str):
    """Gets all unique languages, seasons, and qualities for a series, used for the initial summary."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT DISTINCT language FROM files WHERE normalized_name = ?", (series_name,))
    languages = [row[0] for row in cursor.fetchall()]
    
    cursor.execute("SELECT DISTINCT season FROM files WHERE normalized_name = ? AND season > 0 ORDER BY season ASC", (series_name,))
    seasons = [row[0] for row in cursor.fetchall()]
    
    cursor.execute("SELECT DISTINCT quality FROM files WHERE normalized_name = ?", (series_name,))
    qualities = [row[0] for row in cursor.fetchall()]

    conn.close()

    # Apply custom sort order to qualities
    sort_order = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]
    sorted_qualities = sorted(
        [q for q in qualities if q in sort_order], 
        key=lambda x: sort_order.index(x)
    )
    
    return {
        'languages': languages,
        'seasons': seasons,
        'qualities': sorted_qualities
    }

def search_series_sync(query: str):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()

    q = query.strip()

    # 1) exact match first
    cursor.execute("""
        SELECT DISTINCT normalized_name FROM files 
        WHERE normalized_name = ? COLLATE NOCASE
    """, (q,))
    rows = [r[0] for r in cursor.fetchall()]
    if rows:
        conn.close()
        return rows

    # 2) whole word match only
    pattern = f"% {q} %"
    cursor.execute("""
        SELECT DISTINCT normalized_name FROM files 
        WHERE ' ' || normalized_name || ' ' LIKE ? COLLATE NOCASE
    """, (pattern,))
    rows = [r[0] for r in cursor.fetchall()]

    conn.close()
    return rows

def get_unique_languages_by_name_sync(series_name: str):
    """Gets unique language strings for a given series from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT language FROM files WHERE normalized_name = ? ORDER BY language ASC", (series_name,))
    languages = [row[0] for row in cursor.fetchall()]
    conn.close()
    return languages

def get_unique_seasons_by_name_sync(series_name: str, language: str):
    """Gets unique season numbers for a given series and language from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT season FROM files 
        WHERE normalized_name = ? AND language = ? AND season > 0
        ORDER BY season ASC
    """, (series_name, language))
    seasons = [row[0] for row in cursor.fetchall()]
    conn.close()
    return seasons

def get_unique_qualities_by_season_sync(series_name: str, language: str, season: int):
    """Gets unique quality strings for a given series, language, and season from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT quality FROM files WHERE normalized_name = ? AND language = ? AND season = ?", (series_name, language, season))
    qualities = [row[0] for row in cursor.fetchall()]
    conn.close()

    sort_order = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]
    sorted_qualities = sorted(
        [q for q in qualities if q in sort_order], 
        key=lambda x: sort_order.index(x)
    )
    return sorted_qualities

def get_all_files_by_season_and_quality_sync(series_name: str, season: int, quality: str, language: str):
    """Gets file details filtered by series, season, quality, AND language from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row 
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM files 
        WHERE normalized_name = ? AND season = ? AND quality = ? AND language = ?
        ORDER BY episode ASC
    """, (series_name, season, quality, language))
    files = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return files

def get_unique_languages_by_season_sync(series_name: str, season: int):
    """Gets unique language strings for a given series and season from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT language FROM files 
        WHERE normalized_name = ? AND season = ?
        ORDER BY language ASC
    """, (series_name, season))
    languages = [row[0] for row in cursor.fetchall()]
    conn.close()
    return languages

def get_unique_qualities_by_lang_season_sync(series_name: str, language: str, season: int):
    """Gets unique quality strings for a given series, language, and season from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT quality FROM files WHERE normalized_name = ? AND language = ? AND season = ?", 
        (series_name, language, season)
    )
    qualities = [row[0] for row in cursor.fetchall()]
    conn.close()

    # Apply custom sort order to qualities
    sort_order = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]
    sorted_qualities = sorted(
        [q for q in qualities if q in sort_order], 
        key=lambda x: sort_order.index(x)
    )
    return sorted_qualities


def get_file_by_message_id_and_channel_id_sync(channel_id: int, message_id: int):
    """Synchronously retrieves a single file document based on its message and channel IDs."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row 
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM files
        WHERE channel_id = ? AND message_id = ?
    """, (channel_id, message_id))
    file_entry = cursor.fetchone()
    conn.close()
    return dict(file_entry) if file_entry else None

def save_file_to_sql_sync(file_doc: dict, logger) -> bool:
    """Synchronously inserts/updates a single file document into SQLITE."""
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO files 
            (channel_id, message_id, file_name, normalized_name, season, episode, quality, language, file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            file_doc["channel_id"], file_doc["message_id"], file_doc["file_name"], 
            file_doc["normalized_name"], file_doc["season"], file_doc["episode"], 
            file_doc["quality"], file_doc["language"],
            file_doc.get("file_id")
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Failed to insert/update file doc in SQLite: {e}")
        return False

# --- DELETE AND COUNT FUNCTIONS (SYNC) ---

def delete_all_files_sync():
    """Synchronously deletes all files from sqlite"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM files")
    conn.commit()
    conn.close()

def delete_one_file_sync(channel_id: int, msg_id: int):
    """Synchronously deletes single file from sqlite"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM files WHERE channel_id = ? AND message_id = ?", (channel_id, msg_id))
    conn.commit()
    conn.close()

def get_total_files_sync():
    """Synchronously returns file count from sqlite"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM files")
    total = cursor.fetchone()[0]
    conn.close()
    return total
