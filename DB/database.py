# database.py
import asyncio
import re
from pymongo import MongoClient, TEXT, DESCENDING
# Assuming config.py contains MONGO_URI and logger
from config import MONGO_URI, logger

# --- MONGODB CONFIG & STATE ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
files_collection = db["files"] 
state_collection = db["bot_state"] 
users_collection = db["users"]

# Ensure MongoDB Indexes (for search/backup if needed)
try:
    files_collection.create_index([("normalized_name", 1), ("season", 1)], background=True)
    files_collection.create_index(
        [("normalized_name", TEXT)], 
        name="series_name_text_index", 
        background=True, 
        language_override="text_index_lang", 
    )
    files_collection.create_index([("normalized_name", 1), ("season", 1), ("quality", 1), ("language", 1)], background=True)
    state_collection.create_index([("_id", 1)]) 
    logger.info("✅ MongoDB Indexes ensured/created successfully.")
except Exception as e:
    logger.error(f"❌ Failed to ensure/create MongoDB Indexes: {e}")

# --- SQLITE CONFIG AND IMPORT HOOK ---
# Import necessary SQLite functions from the dedicated file
from sqlite_db import (
    SQLITE_DB_PATH,
    sql_db_lock,
    create_sql_schema,
    migrate_data_from_mongo_to_sql_sync,
    repair_normalized_names_in_sqlite_sync,
    get_series_summary_sync,
    search_series_sync,
    get_unique_languages_by_name_sync,
    get_unique_seasons_by_name_sync,
    get_unique_qualities_by_season_sync,
    get_all_files_by_season_and_quality_sync,
    get_unique_languages_by_season_sync,
    get_unique_qualities_by_lang_season_sync,
    get_file_by_message_id_and_channel_id_sync,
    delete_all_files_sync,
    delete_one_file_sync,
    get_total_files_sync,
    _normalized_series_from_filename, # Import helper for MongoDB save/migration context
)

# --- MIGRATION / INITIALIZATION ---

def initialize_sql_db():
    """Synchronously initializes the SQL database and runs the migration + repair."""
    logger.info("Starting migration from MongoDB to SQLite...")
    migrate_data_from_mongo_to_sql_sync(files_collection, create_sql_schema, logger)
    try:
        repair_normalized_names_in_sqlite_sync(logger)
    except Exception as e:
        logger.error(f"repair_normalized_names_in_sqlite failed: {e}")
    logger.info("SQLite initialization complete.")


# --- BOT STATE FUNCTIONS (MongoDB) ---

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


# --- GLOBAL SETTINGS (MongoDB) ---

# ... set_global_setting and get_global_setting functions remain the same ...
# (omitted for brevity, they are identical to the original)
async def set_global_setting(key: str, value):
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
    GLOBAL_DOC_ID = "GLOBAL_CONFIG"
    def sync_get():
        doc = state_collection.find_one({"_id": GLOBAL_DOC_ID})
        return doc.get(key, default_value) if doc else default_value
    return await asyncio.to_thread(sync_get)


# --- NEW FUNCTION: Get Series Summary (SQLite Async Wrapper) ---

async def get_series_summary(series_name: str):
    """Gets all unique languages, seasons, and qualities for a series."""
    async with sql_db_lock:
        return await asyncio.to_thread(get_series_summary_sync, series_name)


# --- SEARCH AND RETRIEVAL FUNCTIONS (SQLite Async Wrappers) ---

async def search_series(query: str):
    async with sql_db_lock:
        return await asyncio.to_thread(search_series_sync, query)

async def get_unique_languages_by_name(series_name: str):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_languages_by_name_sync, series_name)

async def get_unique_seasons_by_name(series_name: str, language: str):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_seasons_by_name_sync, series_name, language)

async def get_unique_qualities_by_season(series_name: str, language: str, season: int):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_qualities_by_season_sync, series_name, language, season)

async def get_all_files_by_season_and_quality(series_name: str, season: int, quality: str, language: str):
    async with sql_db_lock:
        return await asyncio.to_thread(get_all_files_by_season_and_quality_sync, series_name, season, quality, language)

async def get_unique_languages_by_season(series_name: str, season: int):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_languages_by_season_sync, series_name, season)

async def get_unique_qualities_by_lang_season(series_name: str, language: str, season: int):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_qualities_by_lang_season_sync, series_name, language, season)


# --- NEW FUNCTION for Click-to-Download (SQLite Async Wrapper) ---
async def get_file_by_message_id_and_channel_id(channel_id: int, message_id: int):
    async with sql_db_lock:
        return await asyncio.to_thread(get_file_by_message_id_and_channel_id_sync, channel_id, message_id)
        
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

        # --- 2. Save to SQLITE (using its sync function) ---
        from sqlite_db import save_file_to_sql_sync # Local import to avoid circular dependency
        sql_success = save_file_to_sql_sync(file_doc, logger) 
            
        return sql_success 

    return await asyncio.to_thread(sync_save)

# --- NEW DELETE AND COUNT FUNCTIONS (SQLite Async Wrappers + MongoDB) ---

async def delete_all_files():
    """delete all files from mongo + sqlite"""
    def sync_clear():
        files_collection.delete_many({})
        delete_all_files_sync()
    await asyncio.to_thread(sync_clear)


async def delete_one_file(channel_id: int, msg_id: int):
    """delete single file from both dbs"""
    def sync_delete():
        files_collection.delete_one({"channel_id": channel_id, "message_id": msg_id})
        delete_one_file_sync(channel_id, msg_id)
    await asyncio.to_thread(sync_delete)


async def get_total_files():
    """returns count from sqlite only"""
    async with sql_db_lock:
        return await asyncio.to_thread(get_total_files_sync)

# --- USER FUNCTIONS (MongoDB) ---

# ... add_user, get_all_users, and get_total_users functions remain the same ...
# (omitted for brevity, they are identical to the original)

async def add_user(user_id: int):
    def sync_add():
        users_collection.update_one(
            {"_id": user_id},
            {"$set": {"_id": user_id}},
            upsert=True
        )
    await asyncio.to_thread(sync_add)


async def get_all_users():
    def sync_get():
        return [u["_id"] for u in users_collection.find({})]
    return await asyncio.to_thread(sync_get)

async def get_total_users() -> int:
    def sync_total_users():
        return users_collection.count_documents({}) 
    return await asyncio.to_thread(sync_total_users)
