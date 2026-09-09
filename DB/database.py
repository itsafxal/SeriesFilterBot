"""
database.py — MULTI-DB EDITION  (FIXED)
=========================================
FIXES in this version:

  1. _async_cache MEMORY LEAK FIXED — was an unbounded dict that grew forever
     (one entry per unique query, never evicted).  Now uses a bounded LRU dict
     capped at _ASYNC_CACHE_MAX entries.  Least-recently-used entries are
     evicted when the cap is hit.  Still invalidated on writes (correct behavior).

  2. All other logic unchanged — no functional regressions.
"""

import asyncio
from collections import OrderedDict

# Alternating batch counter for "both" target
import threading as _threading
_batch_counter: int = 0
_batch_counter_lock = _threading.Lock()

from pymongo import MongoClient
from config import (
    MONGO_URI_CONFIG,
    MONGO_URI_DB1,
    MONGO_URI_DB2,
    DATABASE_NAME,
    logger,
)

# ─────────────────────────────────────────────────────────────
# THREE MONGO CLIENTS
# ─────────────────────────────────────────────────────────────

# ── MongoDB connection pool tuning for low-RAM VPS (256 MB) ──
_MONGO_POOL_OPTS = dict(
    maxPoolSize=5,
    minPoolSize=1,
    maxIdleTimeMS=30000,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)

config_client = MongoClient(MONGO_URI_CONFIG, **_MONGO_POOL_OPTS)
config_db      = config_client[DATABASE_NAME]

state_collection    = config_db["bot_state"]
users_collection    = config_db["users"]
pending_collection  = config_db["pending_delivery"]
sticker_collection  = config_db["season_stickers"]
groups_collection   = config_db["groups"]

uri_db1 = MONGO_URI_DB1.strip() if MONGO_URI_DB1 and MONGO_URI_DB1.strip() else MONGO_URI_CONFIG
file_client_1      = MongoClient(uri_db1, **_MONGO_POOL_OPTS)
file_db_1          = file_client_1["SeriesBotFiles1"]
files_collection_1 = file_db_1["files"]

uri_db2 = MONGO_URI_DB2.strip() if MONGO_URI_DB2 and MONGO_URI_DB2.strip() else MONGO_URI_CONFIG
file_client_2      = MongoClient(uri_db2, **_MONGO_POOL_OPTS)
file_db_2          = file_client_2["SeriesBotFiles2"]
files_collection_2 = file_db_2["files"]

# Legacy alias
mongo_client = config_client

# ─────────────────────────────────────────────────────────────
# ASYNC CACHE — LRU, bounded at _ASYNC_CACHE_MAX entries
# FIX: was an unbounded dict — grew forever on high-traffic bots.
# Now evicts the oldest entry when the cap is hit.
# ─────────────────────────────────────────────────────────────

_ASYNC_CACHE_MAX  = 4096   # max cached query results kept in memory
_async_cache: OrderedDict = OrderedDict()


def _cache_get(key):
    """LRU get — moves hit to end (most-recently-used)."""
    if key not in _async_cache:
        return _MISSING
    _async_cache.move_to_end(key)
    return _async_cache[key]


_MISSING = object()   # sentinel


def _cache_set(key, value):
    """LRU set — evicts oldest entry when cap is hit."""
    if key in _async_cache:
        _async_cache.move_to_end(key)
    _async_cache[key] = value
    if len(_async_cache) > _ASYNC_CACHE_MAX:
        _async_cache.popitem(last=False)  # evict LRU


def _invalidate_async_cache():
    """Clear entire cache on writes. Called by clear_all_caches() in sqlite_db."""
    _async_cache.clear()


# ─────────────────────────────────────────────────────────────
# INDEXES
# ─────────────────────────────────────────────────────────────

def _safe_create_index(col, keys, **kwargs):
    index_name = kwargs.get("name") or str(keys)
    try:
        col.create_index(keys, **kwargs)
    except Exception as e:
        code = getattr(e, "code", None) or (
            e.details.get("code") if hasattr(e, "details") else None
        )
        if code == 86:
            logger.info(
                f"ℹ️  Index '{index_name}' already exists on {col.full_name} "
                f"(skipping — existing index is compatible)."
            )
        else:
            logger.error(f"❌ Index creation failed for {col.full_name} [{index_name}]: {e}")


def _ensure_file_indexes(col):
    """Create only essential MongoDB indexes (SQLite FTS5 handles text search)."""
    _safe_create_index(
        col,
        [("normalized_name", 1), ("season", 1)],
        background=True,
    )
    _safe_create_index(
        col,
        [
            ("normalized_name", 1),
            ("season",          1),
            ("episode",         1),
            ("quality",         1),
            ("language",        1),
        ],
        background=True,
        unique=True,
        name="content_identity_unique",
    )
    _safe_create_index(
        col,
        [("channel_id", 1), ("message_id", 1)],
        background=True,
    )


def _ensure_all_indexes_sync():
    try:
        _ensure_file_indexes(files_collection_1)
        _ensure_file_indexes(files_collection_2)
        state_collection.create_index([("_id", 1)])
        try:
            _idx_names = [i["name"] for i in sticker_collection.list_indexes()]
            if "brand_1_season_1" in _idx_names:
                sticker_collection.drop_index("brand_1_season_1")
                logger.info("Dropped legacy sticker index 'brand_1_season_1'")
        except Exception:
            pass
        _safe_create_index(
            sticker_collection,
            [("season", 1), ("kind", 1)],
            unique=True,
            name="season_kind_unique",
        )
        groups_collection.create_index([("_id", 1)])
        logger.info("✅ MongoDB indexes ensured on all 3 clients (background).")
    except Exception as e:
        logger.error(f"[INDEX INIT] Background index creation failed: {e}")


async def ensure_all_indexes():
    await asyncio.to_thread(_ensure_all_indexes_sync)


# ─────────────────────────────────────────────────────────────
# SQLite imports
# ─────────────────────────────────────────────────────────────

from .sqlite_db import (
    SQLITE_DB_PATH,
    sql_db_lock,
    create_sql_schema,
    migrate_data_from_mongo_to_sql_sync,
    repair_normalized_names_in_sqlite_sync,
    repair_codec_from_filenames_sync,
    get_series_summary_sync,
    search_series_sync,
    get_unique_languages_by_name_sync,
    get_unique_seasons_by_name_sync,
    get_unique_qualities_by_season_sync,
    get_all_files_by_season_and_quality_sync,
    get_unique_languages_by_season_sync,
    get_unique_qualities_by_lang_season_sync,
    get_unique_qualities_with_codec_sync,
    get_file_by_message_id_and_channel_id_sync,
    delete_all_files_sync,
    delete_one_file_sync,
    update_file_id_sync,
    get_total_files_sync,
    get_total_series_sync,
    get_all_series_sync,
    _normalized_series_from_filename,
    save_file_to_sql_sync,
    save_file_docs_batch_sync,
    clear_all_caches,
    episode_exists_sync,
)


# ─────────────────────────────────────────────────────────────
# SQL INIT
# ─────────────────────────────────────────────────────────────

def initialize_sql_db():
    logger.info("[INIT] Starting migration from BOTH MongoDB file DBs → SQLite...")

    class _Merged:
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

        def find_one(self, filt=None, projection=None, sort=None):
            doc1 = files_collection_1.find_one(filt or {}, projection or {}, sort=sort)
            doc2 = files_collection_2.find_one(filt or {}, projection or {}, sort=sort)
            if not doc1:
                return doc2
            if not doc2:
                return doc1
            if doc1["_id"] > doc2["_id"]:
                return doc1
            return doc2

    merged = _Merged()
    migrate_data_from_mongo_to_sql_sync(merged, create_sql_schema, logger)

    try:
        repair_normalized_names_in_sqlite_sync(logger)
    except Exception as e:
        logger.error(f"repair_normalized_names_in_sqlite failed: {e}")

    try:
        repair_codec_from_filenames_sync(logger)
    except Exception as e:
        logger.error(f"repair_codec_from_filenames failed: {e}")

    logger.info("✅ SQLite initialisation complete (dual-DB migration + codec repair done).")


# ─────────────────────────────────────────────────────────────
# GLOBAL SETTINGS
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
# BOT STATE
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
    def _sync():
        try:
            state_collection.delete_many({"_id": {"$regex": "^index_"}})
        except Exception as e:
            logger.error(f"delete_all_index_states error: {e}")
    await asyncio.to_thread(_sync)


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
# SERIES QUERIES  (SQLite, with bounded LRU cache)
# ─────────────────────────────────────────────────────────────

async def get_series_summary(series_name: str):
    key = ("summary", series_name)
    hit = _cache_get(key)
    if hit is not _MISSING:
        return hit
    async with sql_db_lock:
        result = await asyncio.to_thread(get_series_summary_sync, series_name)
    _cache_set(key, result)
    return result


async def search_series(query: str):
    key = ("search", query)
    hit = _cache_get(key)
    if hit is not _MISSING:
        return hit
    async with sql_db_lock:
        result = await asyncio.to_thread(search_series_sync, query)
    _cache_set(key, result)
    return result


async def get_unique_languages_by_name(series_name: str):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_languages_by_name_sync, series_name)


async def get_unique_seasons_by_name(series_name: str, language: str):
    async with sql_db_lock:
        return await asyncio.to_thread(get_unique_seasons_by_name_sync, series_name, language)


async def get_unique_qualities_by_season(series_name: str, language: str, season: int):
    async with sql_db_lock:
        return await asyncio.to_thread(
            get_unique_qualities_by_season_sync, series_name, language, season
        )


async def get_all_files_by_season_and_quality(
    series_name: str, season: int, quality: str, language: str
):
    async with sql_db_lock:
        return await asyncio.to_thread(
            get_all_files_by_season_and_quality_sync, series_name, season, quality, language
        )


async def get_unique_languages_by_season(series_name: str, season: int):
    key = ("langs_season", series_name, season)
    hit = _cache_get(key)
    if hit is not _MISSING:
        return hit
    async with sql_db_lock:
        result = await asyncio.to_thread(get_unique_languages_by_season_sync, series_name, season)
    _cache_set(key, result)
    return result


async def get_unique_qualities_by_lang_season(
    series_name: str, language: str, season: int
):
    async with sql_db_lock:
        return await asyncio.to_thread(
            get_unique_qualities_by_lang_season_sync, series_name, language, season
        )


async def get_unique_qualities_with_codec(
    series_name: str, language: str, season: int
):
    key = ("qc", series_name, language, season)
    hit = _cache_get(key)
    if hit is not _MISSING:
        return hit
    async with sql_db_lock:
        result = await asyncio.to_thread(
            get_unique_qualities_with_codec_sync, series_name, language, season
        )
    _cache_set(key, result)
    return result


async def get_file_by_message_id_and_channel_id(channel_id: int, message_id: int):
    async with sql_db_lock:
        return await asyncio.to_thread(
            get_file_by_message_id_and_channel_id_sync, channel_id, message_id
        )


async def episode_exists(
    normalized_name: str, season: int, episode: int, quality: str, language: str
) -> bool:
    async with sql_db_lock:
        return await asyncio.to_thread(
            episode_exists_sync, normalized_name, season, episode, quality, language
        )


# ─────────────────────────────────────────────────────────────
# FILE CRUD — target-aware
# ─────────────────────────────────────────────────────────────

def _mongo_upsert(col, file_doc: dict):
    try:
        codec = (file_doc.get("codec") or "").strip()
        set_fields = {k: v for k, v in file_doc.items() if k not in ("codec", "_id")}
        if codec:
            set_fields["codec"] = codec

        col.update_one(
            {
                "normalized_name": file_doc["normalized_name"],
                "season":          file_doc["season"],
                "episode":         file_doc["episode"],
                "quality":         file_doc["quality"],
                "language":        file_doc["language"],
            },
            {"$set": set_fields},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.error(f"❌ Mongo upsert failed ({col.full_name}): {e}")
        return False


async def save_file_to_db(file_doc: dict, target: str = "both"):
    def _mongo_sync():
        global _batch_counter
        if target in ("both", "mongo"):
            with _batch_counter_lock:
                _batch_counter += 1
                _resolved = "db1" if _batch_counter % 2 == 1 else "db2"
        else:
            _resolved = target

        if _resolved == "db1":
            _mongo_upsert(files_collection_1, file_doc)
        elif _resolved == "db2":
            _mongo_upsert(files_collection_2, file_doc)

    def _sql_sync():
        return save_file_to_sql_sync(file_doc, logger)

    # Fire MongoDB sync in the background
    asyncio.create_task(asyncio.to_thread(_mongo_sync))

    # Wait only for ultra-fast SQLite
    sql_ok = await asyncio.to_thread(_sql_sync)
    
    if sql_ok:
        clear_all_caches()
        _invalidate_async_cache()

    return sql_ok


async def save_file_docs_batch(docs: list[dict], target: str = "both") -> int:
    if not docs:
        return 0

    def _mongo_sync():
        from pymongo import UpdateOne

        def _build_ops(docs_list):
            ops = []
            for doc in docs_list:
                codec      = (doc.get("codec") or "").strip()
                set_fields = {k: v for k, v in doc.items() if k not in ("codec", "_id")}
                if codec:
                    set_fields["codec"] = codec
                ops.append(UpdateOne(
                    {
                        "normalized_name": doc["normalized_name"],
                        "season":          doc["season"],
                        "episode":         doc["episode"],
                        "quality":         doc["quality"],
                        "language":        doc["language"],
                    },
                    {"$set": set_fields},
                    upsert=True,
                ))
            return ops

        if target in ("both", "mongo"):
            global _batch_counter
            with _batch_counter_lock:
                _batch_counter += 1
                _use_db1 = (_batch_counter % 2 == 1)
            if _use_db1:
                try:
                    files_collection_1.bulk_write(_build_ops(docs), ordered=False)
                except Exception as e:
                    logger.error(f"[BATCH] Mongo bulk_write DB1 error: {e}")
            else:
                try:
                    files_collection_2.bulk_write(_build_ops(docs), ordered=False)
                except Exception as e:
                    logger.error(f"[BATCH] Mongo bulk_write DB2 error: {e}")
        elif target == "db1":
            try:
                files_collection_1.bulk_write(_build_ops(docs), ordered=False)
            except Exception as e:
                logger.error(f"[BATCH] Mongo bulk_write DB1 error: {e}")
        elif target == "db2":
            try:
                files_collection_2.bulk_write(_build_ops(docs), ordered=False)
            except Exception as e:
                logger.error(f"[BATCH] Mongo bulk_write DB2 error: {e}")

    def _sql_sync():
        return save_file_docs_batch_sync(docs, logger)

    # Fire MongoDB sync in the background
    asyncio.create_task(asyncio.to_thread(_mongo_sync))

    # Wait only for ultra-fast SQLite
    saved = await asyncio.to_thread(_sql_sync)
    
    if saved:
        clear_all_caches()
        _invalidate_async_cache()
        
    return saved


async def delete_all_files():
    def _sync():
        for col in (files_collection_1, files_collection_2):
            try:
                col.delete_many({})
            except Exception as e:
                logger.error(f"delete_all_files error ({col.full_name}): {e}")
        delete_all_files_sync()
        clear_all_caches()
        _invalidate_async_cache()

    await asyncio.to_thread(_sync)


async def update_file_id(channel_id: int, message_id: int, new_file_id: str):
    def _sync():
        filt   = {"channel_id": channel_id, "message_id": message_id}
        update = {"$set": {"file_id": new_file_id}}
        updated = 0
        for col in (files_collection_1, files_collection_2):
            try:
                r = col.update_one(filt, update)
                updated += r.modified_count
            except Exception as e:
                logger.error(f"update_file_id mongo error ({col.full_name}): {e}")
        update_file_id_sync(channel_id, message_id, new_file_id)
        if updated:
            clear_all_caches()
            _invalidate_async_cache()
        logger.info(f"[FILE_ID_REFRESH] ch={channel_id} msg={message_id} updated={updated}")

    await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# GROUPS TRACKING
# ─────────────────────────────────────────────────────────────

async def add_group(chat_id: int, title: str = "", username: str = ""):
    """Store group with metadata (title, username, joined_at). Safe to upsert repeatedly."""
    def _sync():
        from datetime import datetime
        groups_collection.update_one(
            {"_id": chat_id},
            {"$set": {
                "_id":       chat_id,
                "title":     title or "",
                "username":  username or "",
                "joined_at": datetime.utcnow(),
            }},
            upsert=True,
        )
    await asyncio.to_thread(_sync)


async def remove_group(chat_id: int):
    def _sync():
        groups_collection.delete_one({"_id": chat_id})
    await asyncio.to_thread(_sync)


async def get_total_groups() -> int:
    def _sync():
        try:
            return groups_collection.estimated_document_count()
        except Exception:
            return 0
    return await asyncio.to_thread(_sync)


async def get_all_groups() -> list[int]:
    """Returns list of all tracked group chat_ids for broadcast."""
    def _sync():
        try:
            return [doc["_id"] for doc in groups_collection.find({}, {"_id": 1})]
        except Exception as e:
            logger.error(f"get_all_groups error: {e}")
            return []
    return await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# DB SIZE INFO
# ─────────────────────────────────────────────────────────────

async def get_db_size_info() -> dict:
    def _sync():
        info = {}
        for label, client, dbname in [
            ("config", config_client, "SeriesBotConfig"),
            ("db1",    file_client_1, "SeriesBotFiles1"),
            ("db2",    file_client_2, "SeriesBotFiles2"),
        ]:
            try:
                stats = client[dbname].command("dbStats")
                info[label] = stats.get("dataSize", 0) + stats.get("indexSize", 0)
            except Exception:
                info[label] = 0
        try:
            import os
            from DB.sqlite_db import SQLITE_DB_PATH as _SQL_PATH
            info["sqlite"] = os.path.getsize(_SQL_PATH) if os.path.exists(_SQL_PATH) else 0
        except Exception:
            info["sqlite"] = 0
        return info
    return await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# FILTER / POSTER TOGGLE SETTINGS
# ─────────────────────────────────────────────────────────────

async def get_filter_enabled() -> bool:
    val = await get_global_setting("FILTER_ENABLED", True)
    return bool(val)

async def set_filter_enabled(enabled: bool):
    await set_global_setting("FILTER_ENABLED", enabled)


async def get_poster_enabled() -> bool:
    val = await get_global_setting("POSTER_ENABLED", True)
    return bool(val)

async def set_poster_enabled(enabled: bool):
    await set_global_setting("POSTER_ENABLED", enabled)


async def get_poster_landscape() -> bool:
    val = await get_global_setting("POSTER_LANDSCAPE", False)
    return bool(val)

async def set_poster_landscape(landscape: bool):
    await set_global_setting("POSTER_LANDSCAPE", landscape)


# ─────────────────────────────────────────────────────────────
# CAPTION CLEAN KEYWORDS
# ─────────────────────────────────────────────────────────────

async def get_caption_clean_keywords() -> list:
    val = await get_global_setting("CAPTION_CLEAN_KEYWORDS", None)
    if val is None:
        from config import DEFAULT_CAPTION_CLEAN_KEYWORDS
        return list(DEFAULT_CAPTION_CLEAN_KEYWORDS)
    return list(val) if isinstance(val, list) else []

async def set_caption_clean_keywords(keywords: list):
    await set_global_setting("CAPTION_CLEAN_KEYWORDS", keywords)


# ─────────────────────────────────────────────────────────────
# USERBOT STRING SESSION
# ─────────────────────────────────────────────────────────────

async def get_userbot_string() -> str | None:
    val = await get_global_setting("USERBOT_STRING", None)
    return val if val else None


async def set_userbot_string(string: str):
    await set_global_setting("USERBOT_STRING", string.strip())


async def delete_userbot_string():
    await set_global_setting("USERBOT_STRING", None)


# ─────────────────────────────────────────────────────────────
# SEASON STICKER CACHE
# ─────────────────────────────────────────────────────────────

async def get_cached_sticker(season: int, kind: str = "start") -> str | None:
    def _sync():
        doc = sticker_collection.find_one({"season": season, "kind": kind})
        return doc.get("file_id") if doc else None
    return await asyncio.to_thread(_sync)


async def save_sticker_cache(season: int, file_id: str, kind: str = "start"):
    def _sync():
        sticker_collection.update_one(
            {"season": season, "kind": kind},
            {"$set": {"file_id": file_id}},
            upsert=True,
        )
    await asyncio.to_thread(_sync)


async def clear_sticker_cache():
    def _sync():
        r = sticker_collection.delete_many({})
        logger.info(f"[STICKER_CACHE] cleared {r.deleted_count} entries")
    await asyncio.to_thread(_sync)


async def get_sticker_style(season: int, kind: str) -> int | None:
    def _sync():
        doc = sticker_collection.find_one({"season": season, "kind": kind})
        return doc.get("style_idx") if doc else None
    return await asyncio.to_thread(_sync)


async def save_sticker_style(season: int, kind: str, style_idx: int):
    def _sync():
        sticker_collection.update_one(
            {"season": season, "kind": kind},
            {"$set": {"style_idx": style_idx}},
            upsert=True,
        )
    await asyncio.to_thread(_sync)


async def delete_one_file(channel_id: int, msg_id: int):
    def _sync():
        filt = {"channel_id": channel_id, "message_id": msg_id}
        for col in (files_collection_1, files_collection_2):
            try:
                col.delete_one(filt)
            except Exception as e:
                logger.error(f"delete_one_file error ({col.full_name}): {e}")
        delete_one_file_sync(channel_id, msg_id)
        clear_all_caches()
        _invalidate_async_cache()

    await asyncio.to_thread(_sync)


async def get_total_files() -> int:
    async with sql_db_lock:
        return await asyncio.to_thread(get_total_files_sync)


async def get_total_series() -> int:
    async with sql_db_lock:
        return await asyncio.to_thread(get_total_series_sync)


async def get_all_series() -> list:
    async with sql_db_lock:
        return await asyncio.to_thread(get_all_series_sync)


async def get_total_files_per_db() -> dict:
    def _sync():
        try:
            c1 = files_collection_1.estimated_document_count()
        except Exception:
            c1 = -1
        try:
            c2 = files_collection_2.estimated_document_count()
        except Exception:
            c2 = -1
        return {"db1": c1, "db2": c2}

    return await asyncio.to_thread(_sync)


async def get_files_count_per_channel() -> dict[int, int]:
    from DB.sqlite_db import _get_conn
    import asyncio as _asyncio

    def _sync():
        try:
            conn   = _get_conn()
            cursor = conn.execute(
                "SELECT channel_id, COUNT(*) FROM files GROUP BY channel_id"
            )
            return {int(row[0]): int(row[1]) for row in cursor.fetchall()}
        except Exception:
            return {}

    return await _asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# USER MANAGEMENT
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
            return users_collection.estimated_document_count()
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
# NORMALIZED NAME REPAIR  (MongoDB)
# ─────────────────────────────────────────────────────────────

async def repair_normalized_names_in_mongo():
    def _repair_col(col):
        from pymongo import UpdateOne
        from pymongo.errors import BulkWriteError
        CHUNK = 5000
        updated  = 0
        skipped  = 0
        conflict = 0

        def _flush(ops):
            nonlocal updated, conflict
            if not ops:
                return
            try:
                r = col.bulk_write(ops, ordered=False)
                updated += r.modified_count
            except BulkWriteError as bwe:
                d = bwe.details or {}
                updated += d.get("nModified", 0)
                dup_ids = []
                for err in d.get("writeErrors", []):
                    if err.get("code") == 11000:
                        conflict += 1
                        fid = err.get("op", {}).get("q", {}).get("_id")
                        if fid:
                            dup_ids.append(fid)
                    else:
                        logger.error(
                            f"repair_normalized_names_in_mongo: non-dup error "
                            f"({col.full_name}): {err.get('errmsg')}"
                        )
                if dup_ids:
                    try:
                        dr = col.delete_many({"_id": {"$in": dup_ids}})
                        logger.info(
                            f"[REPAIR] {col.full_name}: deleted {dr.deleted_count} "
                            f"conflicting duplicate docs"
                        )
                    except Exception as de:
                        logger.error(f"[REPAIR] Failed to delete duplicates: {de}")
            except Exception as e:
                logger.error(
                    f"repair_normalized_names_in_mongo: bulk_write error ({col.full_name}): {e}"
                )

        try:
            cursor = col.find(
                {}, {"_id": 1, "file_name": 1, "normalized_name": 1},
                batch_size=CHUNK,
            )
        except Exception as e:
            logger.error(f"repair_normalized_names_in_mongo: cursor open error ({col.full_name}): {e}")
            return 0, 0, 0

        total_docs = 0
        pending    = []
        try:
            for doc in cursor:
                total_docs += 1
                file_name = doc.get("file_name") or ""
                old_norm  = doc.get("normalized_name") or ""
                new_norm  = _normalized_series_from_filename(file_name).lower()
                if new_norm and new_norm != old_norm:
                    pending.append(UpdateOne(
                        {"_id": doc["_id"]},
                        {"$set": {"normalized_name": new_norm}},
                    ))
                if len(pending) >= CHUNK:
                    _flush(pending)
                    pending = []
            _flush(pending)
            skipped = total_docs - updated - conflict
        except Exception as e:
            logger.error(f"repair_normalized_names_in_mongo: iteration error ({col.full_name}): {e}")
        finally:
            try:
                cursor.close()
            except Exception:
                pass

        return updated, skipped, conflict

    def _sync():
        total_updated = 0
        for col in (files_collection_1, files_collection_2):
            u, s, c = _repair_col(col)
            logger.info(
                f"[REPAIR] {col.full_name}: updated={u}, already_correct={s}, "
                f"conflicts_skipped={c}"
            )
            total_updated += u
        if total_updated:
            clear_all_caches()
        return total_updated

    total = await asyncio.to_thread(_sync)
    logger.info(f"[REPAIR] MongoDB normalized_name repair done. Total updated: {total}")
    return total
