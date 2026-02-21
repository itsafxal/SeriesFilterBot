import asyncio
import re
import sqlite3
from functools import lru_cache

BATCH_SIZE = 10000
PROGRESS_STEP = 50000
SUPER_BATCH_SIZE = 100000

SQLITE_DB_PATH = "series_index.db"
sql_db_lock = asyncio.Lock() 

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

    name = file_name

    # Remove extension
    name = re.sub(r"\.(mkv|mp4|avi|mov)$", "", name, flags=re.IGNORECASE)

    # Remove season/episode
    name = re.sub(r"[sS]\d+\s*[eE]\d+", "", name)

    # Remove years
    name = re.sub(r"\b(19|20)\d{2}\b", "", name)

    # Remove quality
    name = re.sub(
        r"\b(4K|1080P|720P|480P|360P|HDRIP|WEB-DL|WEBRIP|BLURAY|HDTV|HEVC|X264|X265)\b",
        "",
        name,
        flags=re.IGNORECASE
    )

    # Remove brackets
    name = re.sub(r"\[.*?\]|\(.*?\)", "", name)

    # Remove symbols
    name = re.sub(r"[-_#@]", " ", name)

    # Remove standalone numbers
    name = re.sub(r"\b\d+\b", "", name)

    # Clean spaces
    name = re.sub(r"\s+", " ", name).strip()

    return name.title()
    
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
    """High-performance migration with progress tracking."""

    conn = sqlite3.connect(SQLITE_DB_PATH)
    create_sql_schema_func(conn)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM files")
    conn.commit()
    logger.info("ℹ️ Cleared SQLite table for fresh migration.")

    doc_count = files_collection.count_documents({})
    logger.info(f"📦 Total Mongo documents found: {doc_count}")

    mongo_docs = files_collection.find({}, {
        "channel_id": 1,
        "message_id": 1,
        "file_name": 1,
        "normalized_name": 1,
        "season": 1,
        "episode": 1,
        "quality": 1,
        "language": 1,
        "file_id": 1,
        "_id": 0
    })

    batch = []
    total_saved_current_collection = 0
    last_progress_log = 0

    for doc in mongo_docs:
        fn = doc.get("file_name", "") or ""
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
            doc.get("file_id")
        ))

        # 🔹 Commit batch
        if len(batch) >= BATCH_SIZE:
            cursor.executemany("""
                INSERT OR IGNORE INTO files
                (channel_id, message_id, file_name, normalized_name,
                 season, episode, quality, language, file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, batch)

            conn.commit()
            total_saved_current_collection += len(batch)
            batch.clear()

        # 🔹 Progress logging
        if total_saved_current_collection - last_progress_log >= PROGRESS_STEP:
            percent = (total_saved_current_collection / doc_count) * 100 if doc_count else 0
            logger.info(
                f"batch inserted: {total_saved_current_collection} / {doc_count} "
                f"({percent:.2f}%)"
            )
            last_progress_log = total_saved_current_collection

        # 🔹 Safety flush for massive DB
        if total_saved_current_collection >= SUPER_BATCH_SIZE:
            conn.commit()

    # Insert remaining
    if batch:
        cursor.executemany("""
            INSERT OR IGNORE INTO files
            (channel_id, message_id, file_name, normalized_name,
             season, episode, quality, language, file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch)

        conn.commit()
        total_saved_current_collection += len(batch)

    logger.info(
        f"✅ Migration completed: {total_saved_current_collection} / {doc_count} files inserted."
    )

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

@lru_cache(maxsize=1024) 
def get_series_summary_sync(series_name: str):
    """Gets all unique languages, seasons, and qualities for a series, used for the initial summary."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT DISTINCT language FROM files WHERE normalized_name = ?", (series_name,))
    languages = tuple([row[0] for row in cursor.fetchall()]) 
    cursor.execute("SELECT DISTINCT season FROM files WHERE normalized_name = ? AND season > 0 ORDER BY season ASC", (series_name,))
    seasons = tuple([row[0] for row in cursor.fetchall()]) 
    
    cursor.execute("SELECT DISTINCT quality FROM files WHERE normalized_name = ?", (series_name,))
    qualities = [row[0] for row in cursor.fetchall()]

    conn.close()

    # Apply custom sort order to qualities
    sort_order = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]
    sorted_qualities = tuple(sorted( 
        [q for q in qualities if q in sort_order], 
        key=lambda x: sort_order.index(x)
    ))
    
    return {
        'languages': languages,
        'seasons': seasons,
        'qualities': sorted_qualities
    }

@lru_cache(maxsize=1024)
def search_series_sync(query: str):
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()

    q = query.strip()
    unique_results = set()
    
    cursor.execute("""
        SELECT DISTINCT normalized_name FROM files 
        WHERE normalized_name = ? COLLATE NOCASE
    """, (q,))
    for r in cursor.fetchall():
        unique_results.add(r[0])

    search_words = q.split()
    full_phrase_pattern = "%" + "%".join(search_words) + "%"

    cursor.execute("""
        SELECT DISTINCT normalized_name FROM files 
        WHERE normalized_name LIKE ? COLLATE NOCASE
    """, (full_phrase_pattern,))
    for r in cursor.fetchall():
        unique_results.add(r[0])

    for word in search_words:
        if len(word) >= 3: 
            cursor.execute("""
                SELECT DISTINCT normalized_name FROM files 
                WHERE normalized_name LIKE ? COLLATE NOCASE
            """, (f"%{word}%",))
            for r in cursor.fetchall():
                unique_results.add(r[0])

    final_results = list(unique_results)
    
    def sort_key(name):
        name_lower = name.lower()
        query_lower = q.lower()
        
        starts_with_exact = 1 if name_lower.startswith(query_lower) else 0
        
        contains_whole_word = 1 if re.search(r'\b' + re.escape(query_lower) + r'\b', name_lower) else 0

        length_diff = abs(len(name_lower) - len(query_lower))

        return (not starts_with_exact, not contains_whole_word, length_diff, len(name_lower))

    final_results.sort(key=sort_key)

    conn.close()
    return tuple(final_results) 

@lru_cache(maxsize=1024) 
def get_unique_languages_by_name_sync(series_name: str):
    """Gets unique language strings for a given series from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT language FROM files WHERE normalized_name = ? ORDER BY language ASC", (series_name,))
    languages = tuple([row[0] for row in cursor.fetchall()]) 
    conn.close()
    return languages

@lru_cache(maxsize=1024) 
def get_unique_seasons_by_name_sync(series_name: str, language: str):
    """Gets unique season numbers for a given series and language from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT season FROM files 
        WHERE normalized_name = ? AND language = ? AND season > 0
        ORDER BY season ASC
    """, (series_name, language))
    seasons = tuple([row[0] for row in cursor.fetchall()]) 
    conn.close()
    return seasons

@lru_cache(maxsize=1024) 
def get_unique_qualities_by_season_sync(series_name: str, language: str, season: int):
    """Gets unique quality strings for a given series, language, and season from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT quality FROM files WHERE normalized_name = ? AND language = ? AND season = ?", (series_name, language, season))
    qualities = [row[0] for row in cursor.fetchall()]
    conn.close()

    sort_order = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]
    sorted_qualities = tuple(sorted( 
        [q for q in qualities if q in sort_order], 
        key=lambda x: sort_order.index(x)
    ))
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

@lru_cache(maxsize=1024) 
def get_unique_languages_by_season_sync(series_name: str, season: int):
    """Gets unique language strings for a given series and season from SQLITE."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT language FROM files 
        WHERE normalized_name = ? AND season = ?
        ORDER BY language ASC
    """, (series_name, season))
    languages = tuple([row[0] for row in cursor.fetchall()]) 
    conn.close()
    return languages

@lru_cache(maxsize=1024) 
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

    sort_order = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]
    sorted_qualities = tuple(sorted( 
        [q for q in qualities if q in sort_order], 
        key=lambda x: sort_order.index(x)
    ))
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
        clear_all_caches() 
        return True
    except Exception as e:
        logger.error(f"Failed to insert/update file doc in SQLite: {e}")
        return False

def clear_all_caches():
    """Clears all lru_caches in this module."""
    get_series_summary_sync.cache_clear()
    search_series_sync.cache_clear()
    get_unique_languages_by_name_sync.cache_clear()
    get_unique_seasons_by_name_sync.cache_clear()
    get_unique_qualities_by_season_sync.cache_clear()
    get_unique_languages_by_season_sync.cache_clear()
    get_unique_qualities_by_lang_season_sync.cache_clear()

def delete_all_files_sync():
    """Synchronously deletes all files from sqlite"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM files")
    conn.commit()
    conn.close()
    clear_all_caches() 

def delete_one_file_sync(channel_id: int, msg_id: int):
    """Synchronously deletes single file from sqlite"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM files WHERE channel_id = ? AND message_id = ?", (channel_id, msg_id))
    conn.commit()
    conn.close()
    clear_all_caches() 

def get_total_files_sync():
    """Synchronously returns file count from sqlite"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM files")
    total = cursor.fetchone()[0]
    conn.close()
    return total
