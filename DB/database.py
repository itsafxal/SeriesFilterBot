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

CODEC FIX (this version):
  - initialize_sql_db() now calls repair_codec_from_filenames_sync() after migration
    to backfill codec for every row that was indexed before codec detection existed.
  - save_file_to_db() passes through the codec field correctly.
  - repair_normalized_names_in_mongo() left unchanged (codec repair is SQLite-only).
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

state_collection    = config_db["bot_state"]
users_collection    = config_db["users"]
pending_collection  = config_db["pending_delivery"]
sticker_collection  = config_db["season_stickers"]   # cached season stickers
groups_collection   = config_db["groups"]               # groups the bot is in

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

def _safe_create_index(col, keys, **kwargs):
    """
    Create a MongoDB index, gracefully handling conflicts.

    Error code 86 (IndexKeySpecsConflict) means an index with the same name
    already exists but with different options (e.g. unique/sparse flags set
    from a previous bot version). The existing index is functionally correct,
    so we log INFO instead of ERROR and move on — no action needed.

    All other errors are logged at ERROR level as before.
    """
    index_name = kwargs.get("name") or str(keys)
    try:
        col.create_index(keys, **kwargs)
    except Exception as e:
        code = getattr(e, "code", None) or (
            e.details.get("code") if hasattr(e, "details") else None
        )
        if code == 86:
            # Index already exists (possibly with different unique/sparse options).
            # The existing index is correct — this is a harmless no-op.
            logger.info(
                f"ℹ️  Index '{index_name}' already exists on {col.full_name} "
                f"(skipping — existing index is compatible)."
            )
        else:
            logger.error(f"❌ Index creation failed for {col.full_name} [{index_name}]: {e}")


def _ensure_file_indexes(col):
    _safe_create_index(
        col,
        [("normalized_name", 1), ("season", 1)],
        background=True,
    )
    _safe_create_index(
        col,
        [("normalized_name", TEXT)],
        name="series_name_text_index",
        background=True,
        language_override="text_index_lang",
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


_ensure_file_indexes(files_collection_1)
_ensure_file_indexes(files_collection_2)
state_collection.create_index([("_id", 1)])
# Drop legacy indexes so we can create the clean season+kind one.
try:
    sticker_collection.drop_index("brand_1_season_1")
except Exception:
    pass
try:
    sticker_collection.drop_index("brand_season_kind_unique")
except Exception:
    pass
_safe_create_index(
    sticker_collection,
    [("season", 1), ("kind", 1)],
    unique=True,
    name="season_kind_unique",
)
groups_collection.create_index([("_id", 1)])
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
    repair_codec_from_filenames_sync,          # ← codec backfill
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
    save_file_docs_batch_sync,                 # ← NEW: batch save
    clear_all_caches,
    episode_exists_sync,
)


# ─────────────────────────────────────────────────────────────
# SQL INIT  —  migrate from BOTH Mongo file DBs
# ─────────────────────────────────────────────────────────────

class _MergedCursor:
    """
    Wraps two pymongo cursors and iterates them as one logical stream.
    Exposes count_documents() by summing both collections.
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
        filt       = filt or {}
        projection = projection or self._proj
        yield from self._col1.find(filt, projection)
        yield from self._col2.find(filt, projection)

    def __iter__(self):
        return self.find()


def initialize_sql_db():
    """
    Synchronously initialises the SQL database.
    Migrates from BOTH Mongo file collections, then runs two repair passes:
      1. normalized_name repair  — fixes series titles from old normalizer
      2. codec repair            — backfills codec for pre-codec-detection rows
    """
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

    merged = _Merged()
    migrate_data_from_mongo_to_sql_sync(merged, create_sql_schema, logger)

    # Repair 1: normalized series names
    try:
        repair_normalized_names_in_sqlite_sync(logger)
    except Exception as e:
        logger.error(f"repair_normalized_names_in_sqlite failed: {e}")

    # Repair 2: codec backfill — fixes rows where codec = '' from old index runs
    try:
        repair_codec_from_filenames_sync(logger)
    except Exception as e:
        logger.error(f"repair_codec_from_filenames failed: {e}")

    logger.info("✅ SQLite initialisation complete (dual-DB migration + codec repair done).")


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
    async with sql_db_lock:
        return await asyncio.to_thread(
            get_unique_languages_by_season_sync, series_name, season
        )


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
    """Returns list of (quality, codec) tuples for building quality buttons."""
    async with sql_db_lock:
        return await asyncio.to_thread(
            get_unique_qualities_with_codec_sync, series_name, language, season
        )


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
    """
    Upsert one file_doc into a pymongo collection.
    Keyed on content identity (normalized_name + season + episode + quality + language).

    CODEC FIX: $set and $setOnInsert must never touch the same field —
    MongoDB raises error code 40 (path conflict) if they do.

    Strategy:
      - If new codec is non-empty  → include it in $set (overwrites blank, keeps good)
      - If new codec is empty      → omit codec from $set entirely so existing value
                                     is preserved on updates; new inserts get '' by
                                     default from the schema.
    No $setOnInsert used — it caused the conflict.
    """
    try:
        codec = (file_doc.get("codec") or "").strip()

        # Build $set — all fields always, codec only when non-empty
        set_fields = {k: v for k, v in file_doc.items() if k != "codec"}
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
    """
    Inserts/updates a single file document.
    target: "db1" | "db2" | "both"
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


async def save_file_docs_batch(docs: list[dict], target: str = "both") -> int:
    """
    Bulk-save a list of already-parsed file_doc dicts.
    Used by the batch indexer — one call saves up to 200 docs.

    Mongo:  one bulk_write per DB (single round-trip)
    SQLite: one executemany transaction (single round-trip)

    Returns the number of docs saved/updated.
    """
    if not docs:
        return 0

    def _sync():
        from pymongo import UpdateOne

        # ── MongoDB bulk_write ────────────────────────────────
        def _build_ops(docs_list):
            ops = []
            for doc in docs_list:
                codec      = (doc.get("codec") or "").strip()
                set_fields = {k: v for k, v in doc.items() if k != "codec"}
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

        ops = _build_ops(docs)

        if target in ("db1", "both"):
            try:
                files_collection_1.bulk_write(ops, ordered=False)
            except Exception as e:
                logger.error(f"[BATCH] Mongo bulk_write DB1 error: {e}")

        if target in ("db2", "both"):
            try:
                files_collection_2.bulk_write(ops, ordered=False)
            except Exception as e:
                logger.error(f"[BATCH] Mongo bulk_write DB2 error: {e}")

        # ── SQLite executemany ────────────────────────────────
        saved = save_file_docs_batch_sync(docs, logger)
        if saved:
            clear_all_caches()
        return saved

    return await asyncio.to_thread(_sync)


async def delete_all_files():
    def _sync():
        for col in (files_collection_1, files_collection_2):
            try:
                col.delete_many({})
            except Exception as e:
                logger.error(f"delete_all_files error ({col.full_name}): {e}")
        delete_all_files_sync()
        clear_all_caches()

    await asyncio.to_thread(_sync)


async def update_file_id(channel_id: int, message_id: int, new_file_id: str):
    """
    Update stale file_id in both MongoDB collections and SQLite.
    Called after a successful copy_message() forward fallback — the forwarded
    message contains a fresh, valid file_id which we persist so future sends
    use send_cached_media() directly without needing the fallback again.
    """
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
        # SQLite update
        update_file_id_sync(channel_id, message_id, new_file_id)
        if updated:
            clear_all_caches()
        logger.info(f"[FILE_ID_REFRESH] ch={channel_id} msg={message_id} updated={updated}")

    await asyncio.to_thread(_sync)




# ─────────────────────────────────────────────────────────────
# GROUPS TRACKING
# ─────────────────────────────────────────────────────────────

async def add_group(chat_id: int):
    def _sync():
        groups_collection.update_one({"_id": chat_id}, {"$set": {"_id": chat_id}}, upsert=True)
    await asyncio.to_thread(_sync)


async def remove_group(chat_id: int):
    def _sync():
        groups_collection.delete_one({"_id": chat_id})
    await asyncio.to_thread(_sync)


async def get_total_groups() -> int:
    def _sync():
        try:
            return groups_collection.count_documents({})
        except Exception:
            return 0
    return await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# DB SIZE INFO
# ─────────────────────────────────────────────────────────────

async def get_db_size_info() -> dict:
    """Return storage sizes for config, db1, db2, and SQLite in bytes."""
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
        # SQLite
        try:
            import os
            from utils.sqlite_db import DB_PATH
            info["sqlite"] = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
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
# SEASON STICKER CACHE
# ─────────────────────────────────────────────────────────────

async def get_cached_sticker(season: int, kind: str = "start") -> str | None:
    """Return cached Telegram file_id for this season+kind, or None."""
    def _sync():
        doc = sticker_collection.find_one({"season": season, "kind": kind})
        return doc.get("file_id") if doc else None
    return await asyncio.to_thread(_sync)


async def save_sticker_cache(season: int, file_id: str, kind: str = "start"):
    """Upsert the file_id for this season+kind."""
    def _sync():
        sticker_collection.update_one(
            {"season": season, "kind": kind},
            {"$set": {"file_id": file_id}},
            upsert=True,
        )
    await asyncio.to_thread(_sync)


async def clear_sticker_cache():
    """Delete all cached stickers."""
    def _sync():
        r = sticker_collection.delete_many({})
        logger.info(f"[STICKER_CACHE] cleared {r.deleted_count} entries")
    await asyncio.to_thread(_sync)


async def get_sticker_style(season: int, kind: str) -> int | None:
    """Return the saved style_idx for this season+kind, or None."""
    def _sync():
        doc = sticker_collection.find_one({"season": season, "kind": kind})
        return doc.get("style_idx") if doc else None
    return await asyncio.to_thread(_sync)


async def save_sticker_style(season: int, kind: str, style_idx: int):
    """Persist the chosen style_idx for this season+kind."""
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
# NORMALIZED NAME REPAIR  (MongoDB — run once after normalizer upgrade)
# ─────────────────────────────────────────────────────────────

async def repair_normalized_names_in_mongo():
    """
    Re-compute normalized_name for every document in both Mongo
    file DBs and write back only rows that changed.
    Safe to run multiple times.

    FIX: bulk_write with ordered=False raises BulkWriteError when the new
    normalized_name causes a duplicate key conflict on content_identity_unique.
    This is expected — two old docs can map to the same new normalized name.
    We catch error code 11000 and count those as skipped (not errors).
    The conflicting duplicate documents are deleted to keep DB clean.
    """
    def _repair_col(col):
        updated  = 0
        skipped  = 0
        conflict = 0

        try:
            docs = list(col.find({}, {"_id": 1, "file_name": 1, "normalized_name": 1}))
        except Exception as e:
            logger.error(f"repair_normalized_names_in_mongo: fetch error ({col.full_name}): {e}")
            return 0, 0, 0

        from pymongo import UpdateOne
        bulk = []
        for doc in docs:
            file_name = doc.get("file_name") or ""
            old_norm  = doc.get("normalized_name") or ""
            new_norm  = _normalized_series_from_filename(file_name).lower()
            if new_norm and new_norm != old_norm:
                bulk.append(UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {"normalized_name": new_norm}},
                ))

        if not bulk:
            skipped = len(docs)
            return updated, skipped, conflict

        try:
            result  = col.bulk_write(bulk, ordered=False)
            updated = result.modified_count
        except Exception as e:
            # BulkWriteError — extract partial results and count conflicts
            from pymongo.errors import BulkWriteError
            if isinstance(e, BulkWriteError):
                details   = e.details or {}
                updated   = details.get("nModified", 0)
                write_errs = details.get("writeErrors", [])
                dup_ids   = []
                for err in write_errs:
                    if err.get("code") == 11000:
                        conflict += 1
                        # The _id of the doc that FAILED to update (it's the duplicate)
                        op = err.get("op", {})
                        failed_id = op.get("q", {}).get("_id")
                        if failed_id:
                            dup_ids.append(failed_id)
                    else:
                        logger.error(
                            f"repair_normalized_names_in_mongo: non-dup error "
                            f"({col.full_name}): {err.get('errmsg')}"
                        )
                # Delete the conflicting duplicates — they are stale old-norm docs
                if dup_ids:
                    try:
                        del_result = col.delete_many({"_id": {"$in": dup_ids}})
                        logger.info(
                            f"[REPAIR] {col.full_name}: deleted {del_result.deleted_count} "
                            f"conflicting duplicate docs"
                        )
                    except Exception as de:
                        logger.error(f"[REPAIR] Failed to delete duplicates: {de}")
            else:
                logger.error(
                    f"repair_normalized_names_in_mongo: bulk_write error ({col.full_name}): {e}"
                )

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
