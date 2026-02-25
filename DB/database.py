"""
database.py — MULTI-DB EDITION
================================
Architecture:
  • CONFIG_DB   (MONGO_URI_CONFIG)  → bot_state, global settings, users, pending_delivery, sessions
  • FILE_DB_1   (MONGO_URI_DB1)     → files collection #1
  • FILE_DB_2   (MONGO_URI_DB2)     → files collection #2
  • SQLite       → local read-optimised index (migrated from BOTH file DBs on every boot)

On boot:
  - migrate_data_from_mongo_to_sql_sync() is called with MERGED documents from DB1 + DB2
  - Duplicates (channel_id + message_id) are handled by UNIQUE constraint (INSERT OR IGNORE)

On live index:
  - save_file_to_db(file_doc, target="both"|"db1"|"db2")
  - Writes to the selected MongoDB file DBs + always writes to SQLite

New env vars required in config.py:
  MONGO_URI_CONFIG   — config/state/user data
  MONGO_URI_DB1      — file store 1
  MONGO_URI_DB2      — file store 2
  (MONGO_URI kept as alias → CONFIG if you don't separate yet)
"""

import asyncio
import re
from pymongo import MongoClient, TEXT
from config import (
    MONGO_URI_CONFIG,
    MONGO_URI_DB1,
    MONGO_URI_DB2,
    logger,
)

# ─────────────────────────────────────────────────────────────
# THREE MONGO CLIENTS
# ─────────────────────────────────────────────────────────────

# 1. Config DB — settings, state, users, sessions, fsub
config_client = MongoClient(MONGO_URI_CONFIG)
config_db      = config_client["SeriesBotConfig"]

state_collection   = config_db["bot_state"]
users_collection   = config_db["users"]
pending_collection = config_db["pending_delivery"]

# 2. File DB 1
file_client_1      = MongoClient(MONGO_URI_DB1)
file_db_1          = file_client_1["SeriesBotFiles1"]
files_collection_1 = file_db_1["files"]

# 3. File DB 2
file_client_2      = MongoClient(MONGO_URI_DB2)
file_db_2          = file_client_2["SeriesBotFiles2"]
files_collection_2 = file_db_2["files"]

# Legacy alias — kept so fsub.py / files.py that import mongo_client still work
mongo_client = config_client

# ─────────────────────────────────────────────────────────────
# INDEXES — FILE DBs
# ─────────────────────────────────────────────────────────────

def _ensure_file_indexes(col):
    try:
        col.create_index(
            [("normalized_name", 1), ("season", 1)], background=True
        )
        col.create_index(
            [("normalized_name", TEXT)],
            name="series_name_text_index",
            background=True,
            language_override="text_index_lang",
        )
        col.create_index(
            [("normalized_name", 1), ("season", 1), ("quality", 1), ("language", 1)],
            background=True,
        )
        col.create_index(
            [("channel_id", 1), ("message_id", 1)],
            background=True,
            unique=True,
            sparse=True,
        )
    except Exception as e:
        logger.error(f"❌ Index creation failed for {col.full_name}: {e}")


_ensure_file_indexes(files_collection_1)
_ensure_file_indexes(files_collection_2)
state_collection.create_index([("_id", 1)])
logger.info("✅ MongoDB indexes ensured on all 3 clients.")

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
    get_total_series_sync,
    get_all_series_sync,
    _normalized_series_from_filename,
    save_file_to_sql_sync,
    clear_all_caches,
)


# ─────────────────────────────────────────────────────────────
# SQL INIT  —  migrate from BOTH Mongo file DBs
# ─────────────────────────────────────────────────────────────

class _MergedCursor:
    """
    Wraps two pymongo cursors and iterates them as one logical stream.
    Exposes count_documents() by summing both collections.
    Used so that migrate_data_from_mongo_to_sql_sync() needs zero changes.
    """

    def __init__(self, col1, col2, projection):
        self._col1 = col1
        self._col2 = col2
        self._proj = projection

    def count_documents(self, filt):
        try:
            return (
                self._col1.count_documents(filt)
                + self._col2.count_documents(filt)
            )
        except Exception:
            return 0

    def find(self, filt=None, projection=None):
        """Yield from col1 first, then col2."""
        filt       = filt or {}
        projection = projection or self._proj
        yield from self._col1.find(filt, projection)
        yield from self._col2.find(filt, projection)

    # Make the object itself iterable (migrate function calls find())
    # We also patch count_documents on the object so the sync fn works:
    def __iter__(self):
        return self.find()


def initialize_sql_db():
    """
    Synchronously initialises the SQL database.
    Migrates from BOTH Mongo file collections (deduplication handled by
    SQLite UNIQUE(channel_id, message_id) → INSERT OR IGNORE).
    """
    logger.info("[INIT] Starting migration from BOTH MongoDB file DBs → SQLite...")

    # Build a single merged pseudo-collection
    class _Merged:
        """Duck-types enough of pymongo Collection for migrate_data_from_mongo_to_sql_sync."""
        def count_documents(self, filt=None):
            filt = filt or {}
            return (
                files_collection_1.count_documents(filt)
                + files_collection_2.count_documents(filt)
            )

        def find(self, filt=None, projection=None):
            filt = filt or {}
            proj = projection or {}
            yield from files_collection_1.find(filt, proj)
            yield from files_collection_2.find(filt, proj)

    merged = _Merged()
    migrate_data_from_mongo_to_sql_sync(merged, create_sql_schema, logger)

    try:
        repair_normalized_names_in_sqlite_sync(logger)
    except Exception as e:
        logger.error(f"repair_normalized_names_in_sqlite failed: {e}")

    logger.info("✅ SQLite initialisation complete (dual-DB migration done).")


# ─────────────────────────────────────────────────────────────
# GLOBAL SETTINGS  (stored in Config DB)
# ─────────────────────────────────────────────────────────────

GLOBAL_DOC_ID = "GLOBAL_CONFIG"


async def set_global_setting(key: str, value):
    def _sync():
        try:
            state_collection.update_one(
                {"_id": GLOBAL_DOC_ID},
                {"$set": {key: value}},
                upsert=True,
            )
            logger.info(f"💾 Global setting '{key}' saved.")
            return True
        except Exception as e:
            logger.error(f"❌ set_global_setting('{key}') failed: {e}")
            return False

    return await asyncio.to_thread(_sync)


async def get_global_setting(key: str, default_value=None):
    def _sync():
        try:
            doc = state_collection.find_one({"_id": GLOBAL_DOC_ID})
            if doc is None:
                return default_value
            return doc.get(key, default_value)
        except Exception as e:
            logger.error(f"❌ get_global_setting('{key}') failed: {e}")
            return default_value

    return await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# BOT STATE  (Config DB)
# ─────────────────────────────────────────────────────────────

async def save_bot_state(chat_id, last_msg_id):
    def _sync():
        try:
            state_collection.update_one(
                {"_id": chat_id},
                {"$set": {"last_indexed_id": last_msg_id}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"save_bot_state error: {e}")

    await asyncio.to_thread(_sync)


async def get_bot_state(chat_id):
    def _sync():
        try:
            doc = state_collection.find_one({"_id": chat_id})
            return doc.get("last_indexed_id") if doc else None
        except Exception as e:
            logger.error(f"get_bot_state error: {e}")
            return None

    return await asyncio.to_thread(_sync)


async def delete_all_index_states():
    keys = await get_all_bot_state_keys()
    for k in keys:
        if k.startswith("index_"):
            await save_bot_state(k, None)


async def get_all_bot_state_keys():
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
    return await get_global_setting("INDEX_CHANNELS", [])


async def set_index_channels(channel_list: list):
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
        return await asyncio.to_thread(
            get_all_files_by_season_and_quality_sync, series_name, season, quality, language
        )


async def get_unique_languages_by_season(series_name: str, season: int):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_languages_by_season_sync, series_name, season)


async def get_unique_qualities_by_lang_season(series_name: str, language: str, season: int):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_qualities_by_lang_season_sync, series_name, language, season)


async def get_file_by_message_id_and_channel_id(channel_id: int, message_id: int):
    async with sql_db_lock:
        return await asyncio.to_thread(
            get_file_by_message_id_and_channel_id_sync, channel_id, message_id
        )


# ─────────────────────────────────────────────────────────────
# FILE CRUD — target-aware
# ─────────────────────────────────────────────────────────────

def _mongo_upsert(col, file_doc: dict):
    """Helper: upsert one file_doc into a pymongo collection."""
    try:
        col.update_one(
            {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
            {"$set": file_doc},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.error(f"❌ Mongo upsert failed ({col.full_name}): {e}")
        return False


async def save_file_to_db(file_doc: dict, target: str = "both"):
    """
    Inserts/updates a single file document.

    target:
        "db1"  → write to FILE_DB_1 + SQLite
        "db2"  → write to FILE_DB_2 + SQLite
        "both" → write to FILE_DB_1 + FILE_DB_2 + SQLite  (default)
    """
    def _sync():
        mongo_ok = False

        if target in ("db1", "both"):
            ok = _mongo_upsert(files_collection_1, file_doc)
            mongo_ok = mongo_ok or ok

        if target in ("db2", "both"):
            ok = _mongo_upsert(files_collection_2, file_doc)
            mongo_ok = mongo_ok or ok

        sql_ok = save_file_to_sql_sync(file_doc, logger)

        if sql_ok or mongo_ok:
            clear_all_caches()

        return sql_ok

    return await asyncio.to_thread(_sync)


async def delete_all_files():
    """Delete all files from both Mongo file DBs + SQLite."""
    def _sync():
        for col in (files_collection_1, files_collection_2):
            try:
                col.delete_many({})
            except Exception as e:
                logger.error(f"delete_all_files error ({col.full_name}): {e}")
        delete_all_files_sync()
        clear_all_caches()

    await asyncio.to_thread(_sync)


async def delete_one_file(channel_id: int, msg_id: int):
    """Delete single file from both Mongo file DBs + SQLite."""
    def _sync():
        filt = {"channel_id": channel_id, "message_id": msg_id}
        for col in (files_collection_1, files_collection_2):
            try:
                col.delete_one(filt)
            except Exception as e:
                logger.error(f"delete_one_file error ({col.full_name}): {e}")
        delete_one_file_sync(channel_id, msg_id)
        clear_all_caches()

    await asyncio.to_thread(_sync)


async def get_total_files() -> int:
    async with sql_db_lock:
        return await asyncio.to_thread(get_total_files_sync)


async def get_total_series() -> int:
    """
    Returns count of distinct normalized_name values = unique series titles.
    This is the "batched series" count shown in /total.
    """
    async with sql_db_lock:
        return await asyncio.to_thread(get_total_series_sync)


async def get_all_series() -> list:
    """
    Returns list of (normalized_name, file_count) tuples sorted alphabetically.
    Used by /listbatch command.
    """
    async with sql_db_lock:
        return await asyncio.to_thread(get_all_series_sync)


# Convenience: total docs in each Mongo file DB
async def get_total_files_per_db() -> dict:
    def _sync():
        try:
            c1 = files_collection_1.count_documents({})
        except Exception:
            c1 = -1
        try:
            c2 = files_collection_2.count_documents({})
        except Exception:
            c2 = -1
        return {"db1": c1, "db2": c2}

    return await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# USER MANAGEMENT  (Config DB)
# ─────────────────────────────────────────────────────────────

async def add_user(user_id: int):
    def _sync():
        try:
            users_collection.update_one(
                {"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True
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
# PENDING DELIVERY  (Config DB)
# ─────────────────────────────────────────────────────────────

async def save_pending_delivery(user_id: int, data: dict):
    def _sync():
        try:
            pending_collection.update_one(
                {"user_id": user_id}, {"$set": data}, upsert=True
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


# ─────────────────────────────────────────────────────────────
# NORMALIZED NAME REPAIR  (run once after normalizer upgrade)
# ─────────────────────────────────────────────────────────────

async def repair_normalized_names_in_mongo():
    """
    Re-compute normalized_name for every document in both Mongo
    file DBs using the current (improved) normalizer and write
    back only rows that changed.

    Called from initialize_sql_db() path in main.py so it runs
    automatically on the first restart after the normalizer update.
    Safe to run multiple times — no-ops when names are already correct.
    """
    from utils.sqlite_db import _normalized_series_from_filename  # shared normalizer

    def _repair_col(col):
        updated = 0
        skipped = 0
        try:
            docs = list(col.find({}, {"_id": 1, "file_name": 1, "normalized_name": 1}))
        except Exception as e:
            logger.error(f"repair_normalized_names_in_mongo: fetch error ({col.full_name}): {e}")
            return 0, 0

        bulk = []
        for doc in docs:
            file_name = doc.get("file_name") or ""
            old_norm  = doc.get("normalized_name") or ""
            new_norm  = _normalized_series_from_filename(file_name).lower()
            if new_norm and new_norm != old_norm:
                from pymongo import UpdateOne
                bulk.append(UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {"normalized_name": new_norm}},
                ))

        if bulk:
            try:
                result = col.bulk_write(bulk, ordered=False)
                updated = result.modified_count
            except Exception as e:
                logger.error(f"repair_normalized_names_in_mongo: bulk_write error ({col.full_name}): {e}")
        else:
            skipped = len(docs)

        return updated, skipped

    def _sync():
        total_updated = 0
        for col in (files_collection_1, files_collection_2):
            u, s = _repair_col(col)
            logger.info(
                f"[REPAIR] {col.full_name}: "
                f"updated={u}, already_correct={s}"
            )
            total_updated += u
        if total_updated:
            clear_all_caches()
        return total_updated

    total = await asyncio.to_thread(_sync)
    logger.info(f"[REPAIR] MongoDB normalized_name repair done. Total updated: {total}")
    return total
