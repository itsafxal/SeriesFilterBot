"""
database.py — Database Layer (FIXED)
======================================
FIXES APPLIED:
  1. get_global_setting always returns default_value when key is missing (not None)
  2. set_global_setting explicitly logs success/failure
  3. mongo_client exposed cleanly for fsub.py to reuse
  4. All async wrappers are thread-safe via asyncio.to_thread
  5. Added clear error logging on every DB operation
"""

import asyncio
import re
from pymongo import MongoClient, TEXT, DESCENDING
from config import MONGO_URI, logger

# ─────────────────────────────────────────────────────────────
# MONGO CLIENT  (sync, shared with fsub.py)
# ─────────────────────────────────────────────────────────────

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]

files_collection   = db["files"]
state_collection   = db["bot_state"]
users_collection   = db["users"]
pending_collection = db["pending_delivery"]

# ─────────────────────────────────────────────────────────────
# INDEXES
# ─────────────────────────────────────────────────────────────

try:
    files_collection.create_index(
        [("normalized_name", 1), ("season", 1)],
        background=True
    )
    files_collection.create_index(
        [("normalized_name", TEXT)],
        name="series_name_text_index",
        background=True,
        language_override="text_index_lang",
    )
    files_collection.create_index(
        [("normalized_name", 1), ("season", 1), ("quality", 1), ("language", 1)],
        background=True
    )
    files_collection.create_index(
        [("channel_id", 1), ("message_id", 1)],
        background=True,
        unique=True,
        sparse=True
    )
    state_collection.create_index([("_id", 1)])
    logger.info("✅ MongoDB Indexes ensured/created successfully.")
except Exception as e:
    logger.error(f"❌ Failed to ensure/create MongoDB Indexes: {e}")

# ─────────────────────────────────────────────────────────────
# SQLite imports
# ─────────────────────────────────────────────────────────────

from .sqlite_db import (
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
    _normalized_series_from_filename,
    save_file_to_sql_sync,
    clear_all_caches
)


# ─────────────────────────────────────────────────────────────
# SQL INIT
# ─────────────────────────────────────────────────────────────

def initialize_sql_db():
    """Synchronously initializes the SQL database and runs migration + repair."""
    logger.info("Starting migration from MongoDB to SQLite...")
    migrate_data_from_mongo_to_sql_sync(files_collection, create_sql_schema, logger)
    try:
        repair_normalized_names_in_sqlite_sync(logger)
    except Exception as e:
        logger.error(f"repair_normalized_names_in_sqlite failed: {e}")
    logger.info("✅ SQLite initialization complete.")


# ─────────────────────────────────────────────────────────────
# GLOBAL SETTINGS  (used by fsub.py, index, etc.)
# ─────────────────────────────────────────────────────────────

GLOBAL_DOC_ID = "GLOBAL_CONFIG"


async def set_global_setting(key: str, value):
    """Sets a global bot configuration setting in MongoDB."""
    def _sync():
        try:
            state_collection.update_one(
                {"_id": GLOBAL_DOC_ID},
                {"$set": {key: value}},
                upsert=True
            )
            logger.info(f"💾 Global setting '{key}' saved successfully.")
            return True
        except Exception as e:
            logger.error(f"❌ set_global_setting('{key}') failed: {e}")
            return False

    return await asyncio.to_thread(_sync)


async def get_global_setting(key: str, default_value=None):
    """
    Retrieves a global bot configuration setting from MongoDB.
    Always returns default_value if the key does not exist — never raises.
    """
    def _sync():
        try:
            doc = state_collection.find_one({"_id": GLOBAL_DOC_ID})
            if doc is None:
                logger.debug(f"get_global_setting('{key}'): GLOBAL_CONFIG doc not found — returning default")
                return default_value
            result = doc.get(key, default_value)
            logger.debug(f"get_global_setting('{key}') = {result!r}")
            return result
        except Exception as e:
            logger.error(f"❌ get_global_setting('{key}') failed: {e}")
            return default_value

    return await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# BOT STATE
# ─────────────────────────────────────────────────────────────

async def save_bot_state(chat_id, last_msg_id):
    """Saves the last processed message ID for a given chat."""
    def _sync():
        try:
            state_collection.update_one(
                {"_id": chat_id},
                {"$set": {"last_indexed_id": last_msg_id}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"save_bot_state error: {e}")

    await asyncio.to_thread(_sync)


async def get_bot_state(chat_id):
    """Retrieves the last processed message ID for a given chat."""
    def _sync():
        try:
            doc = state_collection.find_one({"_id": chat_id})
            return doc.get("last_indexed_id") if doc else None
        except Exception as e:
            logger.error(f"get_bot_state error: {e}")
            return None

    return await asyncio.to_thread(_sync)


async def delete_all_index_states():
    """Delete all index_<chat_id> states so only latest index is active."""
    keys = await get_all_bot_state_keys()
    for k in keys:
        if k.startswith("index_"):
            await save_bot_state(k, None)


async def get_all_bot_state_keys():
    """Return list of all state keys stored."""
    def _sync():
        try:
            return list(state_collection.distinct("_id"))
        except Exception as e:
            logger.error(f"get_all_bot_state_keys error: {e}")
            return []

    return await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# INDEX CHANNELS
# ─────────────────────────────────────────────────────────────

async def get_index_channels():
    """Returns all dynamic index channel IDs stored in DB."""
    return await get_global_setting("INDEX_CHANNELS", [])


async def set_index_channels(channel_list: list):
    """Stores all index channel IDs in DB."""
    await set_global_setting("INDEX_CHANNELS", channel_list)


# ─────────────────────────────────────────────────────────────
# SERIES QUERIES  (SQLite)
# ─────────────────────────────────────────────────────────────

async def get_series_summary(series_name: str):
    async with sql_db_lock:
        return await asyncio.to_thread(get_series_summary_sync, series_name)


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


async def get_file_by_message_id_and_channel_id(channel_id: int, message_id: int):
    async with sql_db_lock:
        return await asyncio.to_thread(get_file_by_message_id_and_channel_id_sync, channel_id, message_id)


# ─────────────────────────────────────────────────────────────
# FILE CRUD
# ─────────────────────────────────────────────────────────────

async def save_file_to_db(file_doc: dict):
    """Inserts/updates a single file document into BOTH MongoDB and SQLite."""
    def _sync():
        mongo_success = False
        try:
            files_collection.update_one(
                {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
                {"$set": file_doc},
                upsert=True
            )
            mongo_success = True
        except Exception as e:
            logger.error(f"❌ MongoDB save_file_to_db error: {e}")

        sql_success = save_file_to_sql_sync(file_doc, logger)

        if sql_success or mongo_success:
            clear_all_caches()

        return sql_success

    return await asyncio.to_thread(_sync)


async def delete_all_files():
    """Delete all files from MongoDB + SQLite."""
    def _sync():
        try:
            files_collection.delete_many({})
        except Exception as e:
            logger.error(f"delete_all_files MongoDB error: {e}")
        delete_all_files_sync()
        clear_all_caches()

    await asyncio.to_thread(_sync)


async def delete_one_file(channel_id: int, msg_id: int):
    """Delete single file from both DBs."""
    def _sync():
        try:
            files_collection.delete_one({"channel_id": channel_id, "message_id": msg_id})
        except Exception as e:
            logger.error(f"delete_one_file MongoDB error: {e}")
        delete_one_file_sync(channel_id, msg_id)
        clear_all_caches()

    await asyncio.to_thread(_sync)


async def get_total_files() -> int:
    """Returns file count from SQLite."""
    async with sql_db_lock:
        return await asyncio.to_thread(get_total_files_sync)


# ─────────────────────────────────────────────────────────────
# USER MANAGEMENT
# ─────────────────────────────────────────────────────────────

async def add_user(user_id: int):
    def _sync():
        try:
            users_collection.update_one(
                {"_id": user_id},
                {"$set": {"_id": user_id}},
                upsert=True
            )
        except Exception as e:
            logger.error(f"add_user error: {e}")

    await asyncio.to_thread(_sync)


async def get_all_users():
    def _sync():
        try:
            return [u["_id"] for u in users_collection.find({})]
        except Exception as e:
            logger.error(f"get_all_users error: {e}")
            return []

    return await asyncio.to_thread(_sync)


async def get_total_users() -> int:
    def _sync():
        try:
            return users_collection.count_documents({})
        except Exception as e:
            logger.error(f"get_total_users error: {e}")
            return 0

    return await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# PENDING DELIVERY
# ─────────────────────────────────────────────────────────────

async def save_pending_delivery(user_id: int, data: dict):
    def _sync():
        try:
            pending_collection.update_one(
                {"user_id": user_id},
                {"$set": data},
                upsert=True
            )
        except Exception as e:
            logger.error(f"save_pending_delivery error: {e}")

    await asyncio.to_thread(_sync)


async def get_pending_delivery(user_id: int):
    def _sync():
        try:
            return pending_collection.find_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"get_pending_delivery error: {e}")
            return None

    return await asyncio.to_thread(_sync)


async def clear_pending_delivery(user_id: int):
    def _sync():
        try:
            pending_collection.delete_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"clear_pending_delivery error: {e}")

    await asyncio.to_thread(_sync)
