# Upgraded database.py
# Cleaned imports, removed repair_normalized_names_in_sqlite_sync,
# fully compatible with upgraded sqlite_db.py fuzzy search engine.

import asyncio
from pymongo import MongoClient
from config import MONGO_URI, logger

# MongoDB Setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
files_collection = db["files"]
state_collection = db["bot_state"]
users_collection = db["users"]

# Ensure basic indexes
try:
    files_collection.create_index([("normalized_name", 1), ("season", 1)], background=True)
    state_collection.create_index([("_id", 1)])
    logger.info("MongoDB Indexes OK.")
except Exception as e:
    logger.error(f"MongoDB index error: {e}")

# SQLite Imports
from .sqlite_db import (
    SQLITE_DB_PATH,
    sql_db_lock,
    create_sql_schema,
    migrate_data_from_mongo_to_sql_sync,
    get_series_summary_sync,
    search_series_sync,
    get_unique_languages_by_name_sync,
    get_unique_seasons_by_name_sync,
    get_unique_qualities_by_season_sync,
    get_all_files_by_season_and_quality_sync,
    get_unique_languages_by_season_sync,
    get_unique_qualities_by_lang_season_sync,
    get_file_by_message_id_and_channel_id_sync,
    save_file_to_sql_sync,
    delete_all_files_sync,
    delete_one_file_sync,
    get_total_files_sync,
    clear_all_caches,
)

# Initialize SQLite

def initialize_sql_db():
    logger.info("Migrating MongoDB → SQLite...")
    migrate_data_from_mongo_to_sql_sync(files_collection, create_sql_schema, logger)
    logger.info("SQLite migration completed.")

# BOT STATE (MongoDB)

async def save_bot_state(chat_id, last_msg_id):
    def sync():
        state_collection.update_one({"_id": chat_id}, {"$set": {"last_indexed_id": last_msg_id}}, upsert=True)
    await asyncio.to_thread(sync)

async def get_bot_state(chat_id):
    def sync():
        doc = state_collection.find_one({"_id": chat_id})
        return doc.get("last_indexed_id") if doc else None
    return await asyncio.to_thread(sync)

# GLOBAL SETTINGS (MongoDB)

async def set_global_setting(key: str, value):
    def sync():
        state_collection.update_one({"_id": "GLOBAL_CONFIG"}, {"$set": {key: value}}, upsert=True)
    await asyncio.to_thread(sync)
    logger.info(f"Global setting {key} = {value}")

async def get_global_setting(key: str, default=None):
    def sync():
        doc = state_collection.find_one({"_id": "GLOBAL_CONFIG"})
        if not doc:
            return default
        return doc.get(key, default)
    return await asyncio.to_thread(sync)

# SUMMARY WRAPPER

async def get_series_summary(series_name: str):
    async with sql_db_lock:
        return await asyncio.to_thread(get_series_summary_sync, series_name)

# SEARCH WRAPPER

async def search_series(query: str):
    async with sql_db_lock:
        return await asyncio.to_thread(search_series_sync, query)

# RETRIEVAL WRAPPERS

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

# SAVE FILE

async def save_file_to_db(file_doc: dict):
    def sync():
        try:
            files_collection.update_one(
                {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
                {"$set": file_doc},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Mongo save error: {e}")

        ok = save_file_to_sql_sync(file_doc, logger)
        if ok:
            clear_all_caches()
        return ok

    return await asyncio.to_thread(sync)

# DELETE FUNCTIONS

async def delete_all_files():
    def sync():
        files_collection.delete_many({})
        delete_all_files_sync()
        clear_all_caches()
    return await asyncio.to_thread(sync)

async def delete_one_file(channel_id: int, msg_id: int):
    def sync():
        files_collection.delete_one({"channel_id": channel_id, "message_id": msg_id})
        delete_one_file_sync(channel_id, msg_id)
        clear_all_caches()
    return await asyncio.to_thread(sync)

# TOTAL FILE COUNT

async def get_total_files():
    async with sql_db_lock:
        return await asyncio.to_thread(get_total_files_sync)

# Mongo Users

async def add_user(user_id: int):
    def sync(): users_collection.update_one({"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True)
    await asyncio.to_thread(sync)

async def get_all_users():
    def sync(): return [u["_id"] for u in users_collection.find({})]
    return await asyncio.to_thread(sync)

async def get_total_users():
    def sync(): return users_collection.count_documents({})
    return await asyncio.to_thread(sync)
