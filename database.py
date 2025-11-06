# database.py
import asyncio
import re
import sqlite3
from pymongo import MongoClient, TEXT, DESCENDING
# Assuming config.py contains MONGO_URI and logger
from config import MONGO_URI, logger 

# --- MONGODB CONFIG & STATE ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
files_collection = db["files"] 
state_collection = db["bot_state"] 

# Ensure MongoDB Indexes (for search/backup if needed)
try:
    files_collection.create_index([("normalized_name", 1), ("season", 1)], background=True)
    files_collection.create_index(
        [("normalized_name", TEXT)], 
        name="series_name_text_index", 
        background=True, 
        language_override="text_index_lang", # 🔥 FIX: Prevents MongoDB from trying to use the value in 'language' field as a stemming language.
    )
    files_collection.create_index([("normalized_name", 1), ("season", 1), ("quality", 1), ("language", 1)], background=True)
    state_collection.create_index([("_id", 1)]) 
    logger.info("✅ MongoDB Indexes ensured/created successfully.")
except Exception as e:
    logger.error(f"❌ Failed to ensure/create MongoDB Indexes: {e}")

# --- SQLITE CONFIG AND MIGRATION ---

SQLITE_DB_PATH = "series_index.db"
sql_db_lock = asyncio.Lock()

# ---- Helpers to recompute normalized series names (no import from utils to avoid cycles)

_CLEAN_TAGS = [
    "4K","1080P","720P","HDRIP","WEB-DL","WEBRIP","BLURAY","HDTV",
    "AMZN","NETFLIX","HULU","DDP5.1","X265","X264","10BIT","HEVC",
    "MKV","AAC","PSA","REPACK","COMPLETE","WEB",
]

def _clean_series_name_local(name: str) -> str:
    name = re.sub(r"[._\s]+", " ", name).strip()
    # remove channel tags/brackets/mentions
    name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", name).strip()
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
            language TEXT NOT NULL,  -- **NEW COLUMN**
            UNIQUE(channel_id, message_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_normalized_season_language ON files (normalized_name, language, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_name_season_quality_language ON files (normalized_name, season, quality, language)")
    conn.commit()
    logger.info("✅ SQLite Schema ensured/created successfully.")

def migrate_data_from_mongo_to_sql():
    """Migrates all data from MongoDB's 'files' collection to SQLite."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    create_sql_schema(conn)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM files")
    conn.commit()
    logger.info("ℹ️ Cleared SQLite table for fresh migration.")

    mongo_docs = files_collection.find({}, {
        "channel_id": 1, "message_id": 1, "file_name": 1, 
        "normalized_name": 1, "season": 1, "episode": 1, 
        "quality": 1, "language": 1, "_id": 0
    })
    insert_data = []
    for doc in mongo_docs:
        fn = doc.get("file_name", "") or ""
        normalized = _normalized_series_from_filename(fn)  # ✅ recompute from filename
        insert_data.append((
            doc.get("channel_id"),
            doc.get("message_id"),
            fn,
            normalized,
            doc.get("season", 0),
            doc.get("episode", 0),
            doc.get("quality"),
            doc.get("language", "Unknown")
        ))

    if insert_data:
        try:
            cursor.executemany("""
                INSERT OR IGNORE INTO files (channel_id, message_id, file_name, normalized_name, season, episode, quality, language)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, insert_data)
            conn.commit()
            logger.info(f"✅ Migrated {len(insert_data)} documents from MongoDB to SQLite.")
        except Exception as e:
            logger.error(f"❌ Failed to execute bulk SQL insert during migration: {e}")
    
    conn.close()

def repair_normalized_names_in_sqlite():
    """
    Recomputes normalized_name for all files in SQLite from the file_name,
    so rows previously saved with episode-title 'series' get fixed.
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

def initialize_sql_db():
    """Synchronously initializes the SQL database and runs the migration + repair."""
    migrate_data_from_mongo_to_sql()
    # Optional: also repair any pre-existing rows that slipped in earlier
    try:
        repair_normalized_names_in_sqlite()
    except Exception as e:
        logger.error(f"repair_normalized_names_in_sqlite failed: {e}")


# --- BOT STATE FUNCTIONS ---

async def save_bot_state(chat_id, last_msg_id):
    """Saves the last processed message ID for a given chat to resume later."""
    def sync_save():
        state_collection.update_one(
            {"_id": chat_id},
            {"$set": {"last_indexed_id": last_msg_id}},
            upsert=True
        )
    await asyncio.to_thread(sync_save)

async def get_bot_state(chat_id):
    """Retrieves the last processed message ID for a given chat."""
    def sync_get():
        doc = state_collection.find_one({"_id": chat_id})
        return doc.get("last_indexed_id") if doc else None
    return await asyncio.to_thread(sync_get)


# --- NEW STATE MANAGEMENT FOR GLOBAL SETTINGS ---

async def set_global_setting(key: str, value):
    """Sets a global bot configuration setting."""
    # Use a fixed document ID for global settings
    GLOBAL_DOC_ID = "GLOBAL_CONFIG"
    def sync_set():
        state_collection.update_one(
            {"_id": GLOBAL_DOC_ID},
            {"$set": {key: value}},
            upsert=True
        )
    await asyncio.to_thread(sync_set)
    logger.info(f"💾 Global setting '{key}' set to: {value}")


async def get_global_setting(key: str, default_value=None):
    """Retrieves a global bot configuration setting."""
    # Use a fixed document ID for global settings
    GLOBAL_DOC_ID = "GLOBAL_CONFIG"
    def sync_get():
        doc = state_collection.find_one({"_id": GLOBAL_DOC_ID})
        return doc.get(key, default_value) if doc else default_value
    return await asyncio.to_thread(sync_get)


# --- NEW FUNCTION: Get Series Summary ---

async def get_series_summary(series_name: str):
    """Gets all unique languages, seasons, and qualities for a series, used for the initial summary."""
    async with sql_db_lock:
        def sync_get_summary():
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

        return await asyncio.to_thread(sync_get_summary)


# --- SEARCH AND RETRIEVAL FUNCTIONS ---

async def search_series(query: str):
    """Searches SQLITE for unique series names matching the query using LIKE."""
    async with sql_db_lock:
        def sync_search():
            conn = sqlite3.connect(SQLITE_DB_PATH)
            cursor = conn.cursor()
            like_query = f"%{'%'.join(query.strip().split())}%"
            cursor.execute("""
                SELECT DISTINCT normalized_name FROM files 
                WHERE normalized_name LIKE ? COLLATE NOCASE
            """, (like_query,))
            results = [row[0] for row in cursor.fetchall()]
            conn.close()
            return results
        return await asyncio.to_thread(sync_search)

async def get_unique_languages_by_name(series_name: str):
    """Gets unique language strings for a given series from SQLITE."""
    async with sql_db_lock:
        def sync_get_languages():
            conn = sqlite3.connect(SQLITE_DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT language FROM files WHERE normalized_name = ? ORDER BY language ASC", (series_name,))
            languages = [row[0] for row in cursor.fetchall()]
            conn.close()
            return languages
        return await asyncio.to_thread(sync_get_languages)

async def get_unique_seasons_by_name(series_name: str, language: str):
    """Gets unique season numbers for a given series and language from SQLITE."""
    async with sql_db_lock:
        def sync_get_seasons():
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
        return await asyncio.to_thread(sync_get_seasons)

async def get_unique_qualities_by_season(series_name: str, language: str, season: int):
    """Gets unique quality strings for a given series, language, and season from SQLITE."""
    async with sql_db_lock:
        def sync_get_qualities():
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
        return await asyncio.to_thread(sync_get_qualities)


async def get_all_files_by_season_and_quality(series_name: str, season: int, quality: str, language: str):
    """Gets file details filtered by series, season, quality, AND language from SQLITE."""
    async with sql_db_lock:
        def sync_get_files():
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
        return await asyncio.to_thread(sync_get_files)

# database.py (Add or ensure these functions exist in the 'SEARCH AND RETRIEVAL FUNCTIONS' section)

async def get_unique_languages_by_season(series_name: str, season: int):
    """Gets unique language strings for a given series and season from SQLITE."""
    async with sql_db_lock:
        def sync_get_languages_by_season():
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
        return await asyncio.to_thread(sync_get_languages_by_season)


async def get_unique_qualities_by_lang_season(series_name: str, language: str, season: int):
    """Gets unique quality strings for a given series, language, and season from SQLITE."""
    async with sql_db_lock:
        def sync_get_qualities():
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
        return await asyncio.to_thread(sync_get_qualities)

# --- NEW FUNCTION for Click-to-Download ---
async def get_file_by_message_id_and_channel_id(channel_id: int, message_id: int):
    """Retrieves a single file document based on its message and channel IDs."""
    async with sql_db_lock:
        def sync_get_file():
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
        return await asyncio.to_thread(sync_get_file)
        
async def save_file_to_db(file_doc: dict):
    """Inserts/updates a single file document into BOTH MongoDB and SQLITE."""
    def sync_save():
        # --- 1. Save to MongoDB ---
        mongo_success = False
        try:
            files_collection.update_one(
                {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
                {"$set": file_doc},
                upsert=True
            )
            mongo_success = True
        except Exception as e:
            logger.error(f"Failed to insert/update file doc in MongoDB: {e}")

        # --- 2. Save to SQLITE ---
        sql_success = False
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO files 
                (channel_id, message_id, file_name, normalized_name, season, episode, quality, language)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_doc["channel_id"], file_doc["message_id"], file_doc["file_name"], 
                file_doc["normalized_name"], file_doc["season"], file_doc["episode"], 
                file_doc["quality"], file_doc["language"]
            ))
            conn.commit()
            conn.close()
            sql_success = True
        except Exception as e:
            logger.error(f"Failed to insert/update file doc in SQLite: {e}")
            
        return sql_success 

    return await asyncio.to_thread(sync_save)
