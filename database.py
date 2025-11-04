# database.py - MODIFIED VERSION for SQLITE with Language

import asyncio
import re
import sqlite3
from pymongo import MongoClient, TEXT, DESCENDING
# Assumes config.py exists
from config import MONGO_URI, logger

# --- MONGODB CONFIG (Retained for state/migration source) ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
files_collection = db["files"] 
state_collection = db["bot_state"]

# Ensure MongoDB Indexes (kept for legacy indexing flow)
# ... (Omitted for brevity, assume indexing logic remains)

# --- SQLITE CONFIG AND MIGRATION ---

SQLITE_DB_PATH = "series_index.db"
sql_db_lock = asyncio.Lock()

def create_sql_schema(conn):
    """Creates the necessary table in the SQLite database."""
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
    # Add indexes for search performance
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_normalized_season_language ON files (normalized_name, language, season)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_name_season_quality ON files (normalized_name, season, quality)") # Still useful
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_language ON files (normalized_name, language)") # **NEW INDEX**
    conn.commit()
    logger.info("✅ SQLite Schema ensured/created successfully (including 'language').")

def migrate_data_from_mongo_to_sql():
    """Migrates all data from MongoDB's 'files' collection to SQLite."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    create_sql_schema(conn)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM files")
    conn.commit()
    logger.info("ℹ️ Cleared SQLite table for fresh migration.")

    mongo_docs = files_collection.find({}, {
        "channel_id": 1, 
        "message_id": 1, 
        "file_name": 1, 
        "normalized_name": 1, 
        "season": 1, 
        "episode": 1, 
        "quality": 1, 
        "language": 1, # **ADDED LANGUAGE**
        "_id": 0
    })
    
    insert_data = []
    for doc in mongo_docs:
        insert_data.append((
            doc.get("channel_id"), 
            doc.get("message_id"), 
            doc.get("file_name"), 
            doc.get("normalized_name"), 
            doc.get("season", 0), 
            doc.get("episode", 0), 
            doc.get("quality"),
            doc.get("language", "Unknown Language") # **ADDED LANGUAGE (with fallback)**
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
    else:
        logger.info("ℹ️ No documents to migrate from MongoDB.")
    
    conn.close()
# ... (initialize_sql_db, save_bot_state, get_bot_state remain the same) ...

# --- NEW SEARCH FUNCTION: Get Unique Languages ---

async def get_unique_languages_by_name(series_name: str):
    """Gets unique language strings for a given series from SQLITE."""
    async with sql_db_lock:
        def sync_get_languages():
            conn = sqlite3.connect(SQLITE_DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT DISTINCT language FROM files 
                WHERE normalized_name = ?
                ORDER BY language ASC
            """, (series_name,))
            
            languages = [row[0] for row in cursor.fetchall()]
            conn.close()
            return languages

        return await asyncio.to_thread(sync_get_languages)

# --- MODIFIED SEARCH FUNCTIONS (All now accept 'language') ---

async def search_series(query: str):
    # This remains the same as it searches for the series name across all languages
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

async def get_unique_seasons_by_name(series_name: str, language: str): # **MODIFIED SIGNATURE**
    """Gets unique season numbers for a given series and language from SQLITE."""
    async with sql_db_lock:
        def sync_get_seasons():
            conn = sqlite3.connect(SQLITE_DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT DISTINCT season FROM files 
                WHERE normalized_name = ? AND language = ? AND season > 0  -- **FILTERED BY LANGUAGE**
                ORDER BY season ASC
            """, (series_name, language))
            
            seasons = [row[0] for row in cursor.fetchall()]
            conn.close()
            return seasons

        return await asyncio.to_thread(sync_get_seasons)

async def get_unique_qualities_by_season(series_name: str, language: str, season: int): # **MODIFIED SIGNATURE**
    """Gets unique quality strings for a given series, language, and season from SQLITE."""
    async with sql_db_lock:
        def sync_get_qualities():
            conn = sqlite3.connect(SQLITE_DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT DISTINCT quality FROM files 
                WHERE normalized_name = ? AND language = ? AND season = ? -- **FILTERED BY LANGUAGE**
            """, (series_name, language, season))
            
            qualities = [row[0] for row in cursor.fetchall()]
            conn.close()

            # Apply custom sort order
            sort_order = ["4K", "1080P", "720P", "480P", "360P", "UNKNOWN"]
            sorted_qualities = sorted(
                [q for q in qualities if q in sort_order], 
                key=lambda x: sort_order.index(x)
            )
            return sorted_qualities
            
        return await asyncio.to_thread(sync_get_qualities)


async def get_all_files_by_season_and_quality(series_name: str, season: int, quality: str, language: str): # **MODIFIED SIGNATURE**
    """Gets file details filtered by series, season, quality, AND language from SQLITE."""
    async with sql_db_lock:
        def sync_get_files():
            conn = sqlite3.connect(SQLITE_DB_PATH)
            conn.row_factory = sqlite3.Row 
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT channel_id, message_id, file_name, episode FROM files 
                WHERE normalized_name = ? AND season = ? AND quality = ? AND language = ? -- **FILTERED BY LANGUAGE**
                ORDER BY episode ASC
            """, (series_name, season, quality, language))
            
            files = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return files

        return await asyncio.to_thread(sync_get_files)
    
async def save_file_to_db(file_doc: dict):
    """Inserts/updates a single file document into BOTH MongoDB and SQLITE (including language)."""
    def sync_save():
        # --- 1. Save to MongoDB ---
        # ... (MongoDB logic remains, ensure it includes the 'language' field)
        
        # --- 2. Save to SQLITE ---
        sql_success = False
        try:
            conn = sqlite3.connect(SQLITE_DB_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO files 
                (channel_id, message_id, file_name, normalized_name, season, episode, quality, language) -- **ADDED language**
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_doc["channel_id"], 
                file_doc["message_id"], 
                file_doc["file_name"], 
                file_doc["normalized_name"], 
                file_doc["season"], 
                file_doc["episode"], 
                file_doc["quality"],
                file_doc["language"] # **ADDED LANGUAGE**
            ))
            conn.commit()
            conn.close()
            sql_success = True
        except Exception as e:
            logger.error(f"Failed to insert/update file doc in SQLite: {e}")
            
        return sql_success # Simplified return for SQLite success

    return await asyncio.to_thread(sync_save)
