"""
files.py — FIXED & OPTIMIZED
=============================
FIXES:
  1. Sessions stored in MongoDB (not in-memory dict) — survives restarts
  2. Progress bar uses \n not <br> — HTML parse mode ignores <br>
  3. clear_session called AFTER queue is fully populated (not before)
  4. auto_delay and custom_caption fetched ONCE per delivery (not per-file)
  5. finish_progress edit uses correct \n separator
  6. send_job properly captures loop variable with default arg
  7. BUG FIX: normalized series name used for DB query — raw series name
     from session payload did not match normalized_name column in SQLite,
     causing get_all_files_by_season_and_quality to always return empty.
"""

import time
import asyncio
import re

from pyrogram import enums
from pyrogram.errors import FloodWait, MessageNotModified

from config import logger
from DB.database import (
    get_all_files_by_season_and_quality,
    get_global_setting,
    mongo_client,
)
from utils.cyber_delete import delete_message_later


# =========================================================
# DEFAULT CAPTION
# =========================================================

DEFAULT_CAPTION = (
    "<b>{series}</b>\n\n"
    "<b>{file_name}</b>\n\n"
    "Season <b>{season}</b> • Episode <b>{episode}</b>\n"
    "{language} • {quality}"
)


# =========================================================
# SESSION STORE
# Strategy:
#   - PRIMARY:  In-memory dict  (instant, zero latency, zero race condition)
#   - BACKUP:   MongoDB         (survives bot restarts, TTL auto-cleanup)
#
# set_session()      → writes memory FIRST (sync, instant), then persists to
#                      MongoDB in background (fire-and-forget is safe here
#                      because memory is always the read source during the
#                      same process lifetime)
#
# get_session_async() → reads memory first, falls back to MongoDB only if
#                       not found (handles restart recovery)
#
# This eliminates the race condition completely:
#   set_session() → memory written synchronously
#   deliver_files_direct() → get_session_async() → reads from memory → always found
# =========================================================

SESSION_TTL  = 600   # 10 minutes
_SESSION_MEM: dict[int, dict] = {}   # uid → {data, expires}

_sessions_col = mongo_client["SeriesBotDB"]["user_sessions"]
try:
    _sessions_col.create_index("expires_at", expireAfterSeconds=0)
except Exception:
    pass


def set_session(user_id: int, data: dict):
    """
    Write to in-memory store SYNCHRONOUSLY (instant, no race).
    Persist to MongoDB in background (for restart recovery).
    """
    from datetime import datetime, timedelta

    uid     = int(user_id)
    expires = time.time() + SESSION_TTL

    # 1. Write memory immediately — this is what deliver_files_direct() reads
    _SESSION_MEM[uid] = {"data": data, "expires": expires}

    # 2. Persist to MongoDB in background (safe fire-and-forget — memory is primary)
    doc = {
        "_id":        uid,
        "data":       data,
        "expires_at": datetime.utcnow() + timedelta(seconds=SESSION_TTL),
    }
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(
                asyncio.to_thread(
                    lambda: _sessions_col.replace_one({"_id": uid}, doc, upsert=True)
                )
            )
    except Exception:
        pass  # Non-fatal — memory copy is always the live source


async def get_session_async(user_id: int):
    """
    Read from memory first (fast, zero latency).
    Fall back to MongoDB only after a restart when memory is empty.
    """
    from datetime import datetime

    uid = int(user_id)

    # ── 1. Memory (primary) ──────────────────────────────────
    entry = _SESSION_MEM.get(uid)
    if entry:
        if time.time() > entry["expires"]:
            _SESSION_MEM.pop(uid, None)
        else:
            return entry["data"]

    # ── 2. MongoDB (restart recovery fallback) ───────────────
    def _sync():
        doc = _sessions_col.find_one({"_id": uid})
        if not doc:
            return None
        if doc.get("expires_at") and doc["expires_at"] < datetime.utcnow():
            _sessions_col.delete_one({"_id": uid})
            return None
        return doc.get("data")

    data = await asyncio.to_thread(_sync)
    if data:
        # Warm memory cache for subsequent calls
        _SESSION_MEM[uid] = {"data": data, "expires": time.time() + SESSION_TTL}
    return data


def get_session(user_id: int):
    """Sync read — checks memory only. For legacy compat."""
    uid   = int(user_id)
    entry = _SESSION_MEM.get(uid)
    if not entry:
        return None
    if time.time() > entry["expires"]:
        _SESSION_MEM.pop(uid, None)
        return None
    return entry["data"]


def clear_session(user_id: int):
    uid = int(user_id)
    _SESSION_MEM.pop(uid, None)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(
                asyncio.to_thread(lambda: _sessions_col.delete_one({"_id": uid}))
            )
    except Exception:
        pass


# =========================================================
# FILENAME HELPERS
# =========================================================

def caption_filename(name: str):
    if not name:
        return "Episode"
    # Remove file extension
    name = re.sub(r"\.(mkv|mp4|avi|mov)$", "", name, flags=re.IGNORECASE)
    # FIX: Remove @mentions (e.g. @RickyChannel) before anything else
    name = re.sub(r"@\S+", "", name)
    # Remove brackets and their contents
    name = re.sub(r"\[.*?\]|\(.*?\)", "", name)
    # Normalize separators
    name = re.sub(r"[._\-]+", " ", name)
    # Remove codec/quality junk
    JUNK = r"\b(bluray|webrip|webdl|web|hdrip|x264|x265|hevc|aac|10bit|ddp5\.1|strife|yts|tgx|rip|hq)\b"
    name = re.sub(JUNK, "", name, flags=re.IGNORECASE)
    name = re.sub(r"\b(4k|1080p|720p|480p|360p)\b", "", name, flags=re.IGNORECASE)
    # Remove S01E08 style markers
    name = re.sub(r"\bS\d{1,3}E\d{1,3}\b", "", name, flags=re.IGNORECASE)
    # Collapse spaces
    name = re.sub(r"\s+", " ", name).strip()
    return name if name else "Episode"


def clean_series_name(series: str):
    if not series:
        return ""
    series = series.replace(".", " ").replace("_", " ")
    return re.sub(r"\s+", " ", series).strip()


def _normalize_for_query(series: str) -> str:
    """
    Reproduce the same normalization applied during indexing so the series
    name from the session payload matches the normalized_name column in SQLite.

    The indexer stores normalized_name as lowercased output of
    _normalized_series_name() from cyber_index.py. We must query with the
    same form — otherwise get_all_files_by_season_and_quality always returns
    empty even though the files exist.
    """
    try:
        from utils.cyber_index import _normalized_series_name
        normalized = _normalized_series_name(series)
        return normalized.lower() if normalized else series.lower()
    except Exception:
        # Fallback: basic cleaning + lowercase
        s = series.replace(".", " ").replace("_", " ")
        s = re.sub(r"\s+", " ", s).strip().lower()
        return s


# =========================================================
# SEND QUEUE + PROGRESS SYSTEM
# =========================================================

USER_SEND_QUEUES: dict = {}
USER_SEND_TASKS:  dict = {}
USER_PROGRESS:    dict = {}

BASE_DELAY           = 1.3
PROGRESS_EDIT_INTERVAL = 3   # seconds between progress edits (avoid FloodWait)


def get_user_queue(user_id):
    if user_id not in USER_SEND_QUEUES:
        USER_SEND_QUEUES[user_id] = asyncio.Queue()
    return USER_SEND_QUEUES[user_id]


def is_sender_running(user_id):
    t = USER_SEND_TASKS.get(user_id)
    return t and not t.done()


async def init_progress(cyber, user_id, total):
    msg = await cyber.send_message(
        user_id,
        f"📦 Sending files...\nProgress: 0 / {total}",
        parse_mode=enums.ParseMode.HTML
    )
    USER_PROGRESS[user_id] = {
        "total":   total,
        "sent":    0,
        "msg_id":  msg.id,
        "last":    time.time(),
    }


async def update_progress(cyber, user_id):
    p = USER_PROGRESS.get(user_id)
    if not p:
        return
    if time.time() - p["last"] < PROGRESS_EDIT_INTERVAL:
        return
    try:
        await cyber.edit_message_text(
            user_id,
            p["msg_id"],
            f"📦 Sending files...\nProgress: <b>{p['sent']}</b> / <b>{p['total']}</b>",
            parse_mode=enums.ParseMode.HTML
        )
        p["last"] = time.time()
    except MessageNotModified:
        pass
    except Exception:
        pass


async def finish_progress(cyber, user_id):
    p = USER_PROGRESS.pop(user_id, None)
    if not p:
        return
    try:
        await cyber.edit_message_text(
            user_id,
            p["msg_id"],
            f"✅ <b>Delivery complete!</b>\nSent: <b>{p['total']}</b> files",
            parse_mode=enums.ParseMode.HTML
        )
        delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
        if delay and int(delay) > 0:
            delete_message_later(cyber, user_id, p["msg_id"], int(delay))
    except Exception:
        pass


async def sender_worker(cyber, user_id, on_complete=None):
    """
    on_complete: optional async callable, invoked AFTER all files are sent
    and finish_progress completes. This is how do_deliver() in fsub.py sends
    the retry button at exactly the right moment — after delivery, not before.
    """
    queue = get_user_queue(user_id)

    while not queue.empty():
        job = await queue.get()
        try:
            await job()
            prog = USER_PROGRESS.get(user_id)
            if prog:
                prog["sent"] += 1
                await update_progress(cyber, user_id)
            await asyncio.sleep(BASE_DELAY)

        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"FloodWait {wait}s for uid={user_id}")
            await asyncio.sleep(wait + 1)

        except Exception as e:
            logger.error(f"SEND ERROR uid={user_id}: {e}")
            await asyncio.sleep(1)

        finally:
            queue.task_done()

    await finish_progress(cyber, user_id)
    USER_SEND_TASKS.pop(user_id, None)
    USER_SEND_QUEUES.pop(user_id, None)

    # Fire on_complete AFTER everything done — correct place for retry button
    if on_complete:
        try:
            await on_complete()
        except Exception as e:
            logger.error(f"on_complete error uid={user_id}: {e}")


# =========================================================
# DELIVERY LOGIC
# =========================================================

async def deliver_files_direct(cyber, user_id, on_complete=None):
    """
    on_complete: optional async callable fired after ALL files are sent.
    Pass this from do_deliver() in fsub.py to send the retry button
    only after sender_worker finishes — not immediately.
    """
    session = await get_session_async(user_id)
    if not session:
        logger.warning(f"deliver_files_direct: no session for uid={user_id}")
        return

    raw_series = session["series"]
    series     = clean_series_name(raw_series)
    season     = session["season"]
    language   = session["language"]
    quality    = session["quality"]

    # FIX: normalize the series name to match the normalized_name column in SQLite.
    # The payload stores the series name as returned by encode_payload (which gets
    # it from the filters UI — the display name, not the normalized DB key).
    # The DB always stores/queries by normalized_name (lowercased, junk-stripped).
    # Without this fix, the query always returns empty → no files delivered.
    query_name = _normalize_for_query(raw_series)

    logger.info(
        f"deliver_files_direct: uid={user_id} raw={raw_series!r} "
        f"query={query_name!r} season={season} lang={language} quality={quality}"
    )

    files = await get_all_files_by_season_and_quality(
        query_name, season, quality, language
    )

    if not files:
        logger.warning(
            f"deliver_files_direct: no files found for uid={user_id} "
            f"query={query_name!r} season={season} lang={language} quality={quality}"
        )
        await cyber.send_message(user_id, "❌ No files found.")
        clear_session(user_id)
        return

    logger.info(f"deliver_files_direct: found {len(files)} files for uid={user_id}")

    # FIX: fetch settings ONCE, not per-file
    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
    try:
        auto_delay = int(auto_delay) if auto_delay else 0
    except (TypeError, ValueError):
        auto_delay = 0

    custom_caption = await get_global_setting("CUSTOM_FILE_CAPTION", None)
    if not custom_caption:
        custom_caption = DEFAULT_CAPTION

    await init_progress(cyber, user_id, len(files))

    queue = get_user_queue(user_id)

    for f in files:

        async def send_job(f=f):  # default arg captures loop variable correctly
            if not f.get("file_id"):
                return

            raw_name   = f.get("file_name", "")
            clean_name = caption_filename(raw_name)
            episode    = str(f.get("episode", 0)).zfill(2)
            season_str = str(season).zfill(2)

            try:
                caption = custom_caption.format(
                    series=series,
                    file_name=clean_name,
                    season=season_str,
                    episode=episode,
                    language=language,
                    quality=quality,
                )
            except Exception as e:
                logger.warning(f"Caption format error: {e} — using fallback")
                caption = f"<b>{series}</b>\nS{season_str}E{episode} • {language} • {quality}"

            sent_msg   = None
            media_type = f.get("media_type")

            try:
                if media_type == "video":
                    sent_msg = await cyber.send_video(
                        user_id, f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.HTML
                    )
                elif media_type == "audio":
                    sent_msg = await cyber.send_audio(
                        user_id, f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.HTML
                    )
                else:
                    sent_msg = await cyber.send_document(
                        user_id, f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.HTML
                    )
            except Exception as e:
                logger.error(f"SEND FAILED uid={user_id} file={f.get('file_name')}: {e}")
                return

            if sent_msg and auto_delay > 0:
                delete_message_later(cyber, user_id, sent_msg.id, auto_delay)

        await queue.put(send_job)

    # Clear session AFTER populating queue (not before)
    clear_session(user_id)

    if not is_sender_running(user_id):
        USER_SEND_TASKS[user_id] = asyncio.create_task(
            sender_worker(cyber, user_id, on_complete=on_complete)
        )
