# database.py

import asyncio
import re
from pymongo import MongoClient, TEXT, DESCENDING
import aiosqlite # New Import
# Assumes config.py exists
from config import MONGO_URI, logger 

# ... (Existing MongoDB client and collections remain) ...

# --- NEW: SQLite Initialization ---
SQLITE_DB_PATH = "bot_files.db" # Define your SQLite file name

async def init_sqlite_db():
    """Initializes the SQLite database and creates the 'files' table."""
    try:
        # Use context manager for connection to ensure it closes
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            # Table structure based on file_doc fields in save_file_to_db
            await db.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    channel_id INTEGER,
                    message_id INTEGER,
                    normalized_name TEXT NOT NULL,
                    season INTEGER,
                    episode INTEGER,
                    quality TEXT,
                    file_name TEXT,
                    PRIMARY KEY (channel_id, message_id)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_series_season ON files (normalized_name, season)")
            # You may want a FTS index for search, but let's keep it simple for migration
            await db.commit()
        logger.info("✅ SQLite Database and 'files' table ensured/created successfully.")
    except Exception as e:
        logger.error(f"❌ Failed to initialize SQLite DB: {e}")


# --- NEW: Migration Function ---

async def migrate_mongo_to_sqlite():
    """Migrates all documents from MongoDB's files_collection to SQLite."""
    await init_sqlite_db() # Ensure the table exists
    
    # 1. Get all file documents from MongoDB
    def sync_fetch_all_files():
        # Fetching only the fields relevant to the SQLite table
        return list(files_collection.find({}, {
            "channel_id": 1, 
            "message_id": 1, 
            "normalized_name": 1, 
            "season": 1, 
            "episode": 1, 
            "quality": 1,
            "file_name": 1,
            "_id": 0 # Exclude MongoDB's ObjectId
        }))
    
    mongo_files = await asyncio.to_thread(sync_fetch_all_files)
    
    if not mongo_files:
        logger.info("No files found in MongoDB to migrate.")
        return

    logger.info(f"Starting migration of {len(mongo_files)} files to SQLite...")

    # 2. Insert into SQLite in a batch
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            # Prepare the data for batch insertion
            data_to_insert = [
                (doc.get("channel_id"), doc.get("message_id"), doc.get("normalized_name"), 
                 doc.get("season"), doc.get("episode"), doc.get("quality"), doc.get("file_name"))
                for doc in mongo_files
            ]

            # Use executemany for efficiency
            await db.executemany("""
                INSERT OR REPLACE INTO files 
                (channel_id, message_id, normalized_name, season, episode, quality, file_name) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, data_to_insert)
            
            await db.commit()
            logger.info(f"✅ Successfully migrated {len(data_to_insert)} files to SQLite.")

    except Exception as e:
        logger.error(f"❌ Failed during MongoDB to SQLite migration: {e}")


# --- BOT STATE FUNCTIONS (Remain the same, as they use the existing state_collection) ---
# ... (save_bot_state and get_bot_state remain unchanged) ...


# --- SEARCH AND RETRIEVAL FUNCTIONS (Example of refactor to use SQLite for files) ---

async def get_unique_seasons_by_name(series_name: str):
    """Gets unique season numbers for a given series, now using SQLite."""
    async with aiosqlite.connect(SQLITE_DB_PATH) as db:
        # Use LIKE for case-insensitive partial match similar to your regex
        cursor = await db.execute(
            "SELECT DISTINCT season FROM files WHERE normalized_name LIKE ? AND season > 0 ORDER BY season ASC", 
            (f"{series_name}%",) 
        )
        seasons = [row[0] for row in await cursor.fetchall()]
        return seasons

# ... (Refactor other retrieval functions like get_unique_qualities_by_season and 
# get_all_files_by_season_and_quality to use aiosqlite as well) ...

# NOTE: Since search_series uses MongoDB's $text index, you might keep it in Mongo
# or implement a Full-Text Search (FTS) table in SQLite for a full transition.

# --- SAVE FILE FUNCTION (Now saves to BOTH MongoDB and SQLite) ---

async def save_file_to_db(file_doc: dict):
    """Inserts/updates a single file document into MongoDB and SQLite."""
    
    # 1. Save to MongoDB (Existing logic)
    def sync_insert_one():
        # ... (Existing MongoDB update_one logic) ...
        try:
            # Use channel_id and message_id as a unique compound key for upsert
            result = files_collection.update_one(
                {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
                {"$set": file_doc},
                upsert=True
            )
            return bool(result.upserted_id or result.modified_count > 0)
        except Exception as e:
            logger.error(f"Failed to insert/update file doc in MongoDB: {e}")
            return False
            
    mongo_success = await asyncio.to_thread(sync_insert_one)
    
    # 2. Save to SQLite
    try:
        async with aiosqlite.connect(SQLITE_DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO files 
                (channel_id, message_id, normalized_name, season, episode, quality, file_name) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                file_doc["channel_id"], file_doc["message_id"], file_doc["normalized_name"], 
                file_doc["season"], file_doc["episode"], file_doc["quality"], file_doc["file_name"]
            ))
            await db.commit()
            sqlite_success = True
    except Exception as e:
        logger.error(f"Failed to insert/update file doc in SQLite: {e}")
        sqlite_success = False

    return mongo_success and sqlite_success
