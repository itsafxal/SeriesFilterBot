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
  8. BUG FIX: MEDIA_EMPTY fallback — when file_id is stale/invalid, falls back
     to copy_message() from the source channel using channel_id + message_id.
     This handles re-added bots, rotated file_ids, and cross-session indexing.
"""

import time
import asyncio
import re

from pyrogram import enums
from pyrogram.errors import FloodWait, MessageNotModified, BadRequest

from config import logger, STICKER_ENABLED, PROGRESS_MESSAGE_ENABLED
from DB.database import (
    get_all_files_by_season_and_quality,
    get_global_setting,
    mongo_client,
    update_file_id,
    get_cached_sticker,
    save_sticker_cache,
    clear_sticker_cache,
    get_sticker_style,
    save_sticker_style,
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
# =========================================================

SESSION_TTL  = 600   # 10 minutes
_SESSION_MEM: dict[int, dict] = {}

_sessions_col = mongo_client["SeriesBotDB"]["user_sessions"]
try:
    _sessions_col.create_index("expires_at", expireAfterSeconds=0)
except Exception:
    pass


def set_session(user_id: int, data: dict):
    from datetime import datetime, timedelta

    uid     = int(user_id)
    expires = time.time() + SESSION_TTL

    _SESSION_MEM[uid] = {"data": data, "expires": expires}

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
        pass


async def get_session_async(user_id: int):
    from datetime import datetime

    uid = int(user_id)

    entry = _SESSION_MEM.get(uid)
    if entry:
        if time.time() > entry["expires"]:
            _SESSION_MEM.pop(uid, None)
        else:
            return entry["data"]

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
        _SESSION_MEM[uid] = {"data": data, "expires": time.time() + SESSION_TTL}
    return data


def get_session(user_id: int):
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
    name = re.sub(r"\.(mkv|mp4|avi|mov)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"@\S+", "", name)
    name = re.sub(r"\[.*?\]|\(.*?\)", "", name)
    name = re.sub(r"[._\-]+", " ", name)
    JUNK = r"\b(bluray|webrip|webdl|web|hdrip|x264|x265|hevc|aac|10bit|ddp5\.1|strife|yts|tgx|rip|hq)\b"
    name = re.sub(JUNK, "", name, flags=re.IGNORECASE)
    name = re.sub(r"\b(4k|1080p|720p|480p|360p)\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\bS\d{1,3}E\d{1,3}\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name if name else "Episode"


def clean_series_name(series: str):
    if not series:
        return ""
    series = series.replace(".", " ").replace("_", " ")
    return re.sub(r"\s+", " ", series).strip()


def _normalize_for_query(series: str) -> str:
    try:
        from utils.cyber_series import _normalize_title
        normalized = _normalize_title(series)
        return normalized.lower() if normalized else series.lower()
    except Exception:
        pass
    try:
        from utils.cyber_index import _normalized_series_name
        normalized = _normalized_series_name(series)
        return normalized.lower() if normalized else series.lower()
    except Exception:
        pass
    s = series.replace(".", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


# =========================================================
# SEND QUEUE + PROGRESS SYSTEM
# =========================================================

USER_SEND_QUEUES: dict = {}
USER_SEND_TASKS:  dict = {}
USER_PROGRESS:    dict = {}

BASE_DELAY             = 1.3
PROGRESS_EDIT_INTERVAL = 3


def get_user_queue(user_id):
    if user_id not in USER_SEND_QUEUES:
        USER_SEND_QUEUES[user_id] = asyncio.Queue()
    return USER_SEND_QUEUES[user_id]


def is_sender_running(user_id):
    t = USER_SEND_TASKS.get(user_id)
    return t and not t.done()


async def init_progress(cyber, user_id, total, season: int = 1):
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
        "season":  season,
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

    # Delete the "📦 Sending files... Progress: X/Y" message
    try:
        await cyber.delete_messages(user_id, p["msg_id"])
    except Exception:
        pass

    # Send end sticker if enabled
    if STICKER_ENABLED:
        try:
            from utils.cyber_sticker import make_end_sticker
            season = p.get("season", 1)
            total  = p.get("total",  0)

            # Try cached end sticker file_id
            cached_fid = await get_cached_sticker(season, kind="end")
            end_msg = None
            if cached_fid:
                try:
                    end_msg = await cyber.send_sticker(user_id, cached_fid)
                except Exception:
                    cached_fid = None

            if not cached_fid:
                style_idx = await get_sticker_style(season, kind="end")
                buf, chosen_idx = make_end_sticker(season, total, style_idx=style_idx)
                end_msg = await cyber.send_sticker(user_id, buf)
                logger.info(f"[STICKER] Sent fresh end sticker Season {season:02d} (style={chosen_idx})")
                media = (getattr(end_msg, "sticker", None)
                         or getattr(end_msg, "document", None))
                if media and getattr(media, "file_id", None):
                    await save_sticker_cache(season, media.file_id, kind="end")
                if style_idx is None:
                    await save_sticker_style(season, kind="end", style_idx=chosen_idx)

            delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
            if delay and int(delay) > 0 and end_msg:
                delete_message_later(cyber, user_id, end_msg.id, int(delay))
        except Exception as _e:
            logger.warning(f"[STICKER] End sticker failed: {_e}")


async def sender_worker(cyber, user_id, on_complete=None):
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

    if on_complete:
        try:
            await on_complete()
        except Exception as e:
            logger.error(f"on_complete error uid={user_id}: {e}")


# =========================================================
# FORWARD FALLBACK
# =========================================================

async def _forward_fallback(cyber, user_id: int, f: dict, caption: str, auto_delay: int) -> bool:
    """
    Fallback for MEDIA_EMPTY: copy the original message from the source channel.
    Uses channel_id + message_id stored during indexing.
    Returns True on success, False if fallback also fails.
    """
    channel_id = f.get("channel_id")
    message_id = f.get("message_id")

    if not channel_id or not message_id:
        logger.warning(
            f"[FORWARD_FALLBACK] No channel_id/message_id for file={f.get('file_name')} — cannot forward"
        )
        return False

    try:
        sent_msg = await cyber.copy_message(
            chat_id=user_id,
            from_chat_id=channel_id,
            message_id=message_id,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )
        if sent_msg and auto_delay > 0:
            delete_message_later(cyber, user_id, sent_msg.id, auto_delay)
        logger.info(
            f"[FORWARD_FALLBACK] ✅ Forwarded via copy_message: "
            f"ch={channel_id} msg={message_id} → uid={user_id}"
        )
        # Extract fresh file_id from the forwarded message and persist it
        # so future sends use send_cached_media() directly (no fallback needed)
        try:
            media = (
                getattr(sent_msg, "document", None)
                or getattr(sent_msg, "video", None)
                or getattr(sent_msg, "audio", None)
            )
            if media and media.file_id:
                asyncio.create_task(
                    update_file_id(channel_id, message_id, media.file_id)
                )
                logger.info(
                    f"[FILE_ID_REFRESH] queued refresh for "
                    f"ch={channel_id} msg={message_id}"
                )
        except Exception as e:
            logger.warning(f"[FILE_ID_REFRESH] extract error: {e}")
        return True
    except Exception as e:
        logger.error(
            f"[FORWARD_FALLBACK] ❌ copy_message failed: "
            f"ch={channel_id} msg={message_id} uid={user_id}: {e}"
        )
        return False


# =========================================================
# DELIVERY LOGIC
# =========================================================

async def deliver_files_direct(cyber, user_id, on_complete=None):
    session = await get_session_async(user_id)
    if not session:
        logger.warning(f"deliver_files_direct: no session for uid={user_id}")
        return

    raw_series = session["series"]
    series     = clean_series_name(raw_series)
    season     = session["season"]
    language   = session["language"]
    quality    = session["quality"]

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

    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
    try:
        auto_delay = int(auto_delay) if auto_delay else 0
    except (TypeError, ValueError):
        auto_delay = 0

    custom_caption = await get_global_setting("CUSTOM_FILE_CAPTION", None)
    if not custom_caption:
        custom_caption = DEFAULT_CAPTION

    if PROGRESS_MESSAGE_ENABLED:
        await init_progress(cyber, user_id, len(files), season=season)

    # ── Send season sticker before files ──────────────────────────────
    if STICKER_ENABLED:
        sticker_msg = None
        try:
            # Lazy import — keeps files.py loadable even if Pillow is missing
            from utils.cyber_sticker import make_start_sticker

            # Try cached file_id first
            cached_fid = await get_cached_sticker(season, kind="start")
            if cached_fid:
                try:
                    sticker_msg = await cyber.send_sticker(user_id, cached_fid)
                    logger.info(f"[STICKER] Sent cached start sticker Season {season:02d}")
                except Exception:
                    cached_fid = None   # stale — regenerate below

            if not cached_fid:
                style_idx = await get_sticker_style(season, kind="start")
                sticker_buf, chosen_idx = make_start_sticker(season, style_idx=style_idx)
                sticker_msg = await cyber.send_sticker(user_id, sticker_buf)
                logger.info(f"[STICKER] Sent fresh start sticker Season {season:02d} (style={chosen_idx})")
                media = (getattr(sticker_msg, "sticker", None)
                         or getattr(sticker_msg, "document", None))
                if media and getattr(media, "file_id", None):
                    await save_sticker_cache(season, media.file_id, kind="start")
                    logger.info(f"[STICKER_CACHE] Saved Season {season:02d} start sticker")
                if style_idx is None:
                    await save_sticker_style(season, kind="start", style_idx=chosen_idx)

            if auto_delay > 0 and sticker_msg:
                delete_message_later(cyber, user_id, sticker_msg.id, auto_delay)

        except Exception as _se:
            logger.warning(f"[STICKER] Failed to send season sticker: {_se}")
            # Never block file delivery due to sticker failure

    queue = get_user_queue(user_id)

    for f in files:

        async def send_job(f=f):  # default arg captures loop variable correctly
            if not f.get("file_id") and not (f.get("channel_id") and f.get("message_id")):
                logger.warning(f"[SEND] No file_id or source for {f.get('file_name')} — skipping")
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

            # ── Attempt 1: send by file_id ────────────────────
            sent_msg   = None
            media_type = f.get("media_type")
            file_id    = f.get("file_id")

            if file_id:
                try:
                    if media_type == "video":
                        sent_msg = await cyber.send_video(
                            user_id, file_id,
                            caption=caption,
                            parse_mode=enums.ParseMode.HTML
                        )
                    elif media_type == "audio":
                        sent_msg = await cyber.send_audio(
                            user_id, file_id,
                            caption=caption,
                            parse_mode=enums.ParseMode.HTML
                        )
                    else:
                        sent_msg = await cyber.send_document(
                            user_id, file_id,
                            caption=caption,
                            parse_mode=enums.ParseMode.HTML
                        )
                except BadRequest as e:
                    err_str = str(e)
                    if "MEDIA_EMPTY" in err_str or "FILE_ID_INVALID" in err_str or "FILE_REFERENCE_EXPIRED" in err_str:
                        # ── Attempt 2: forward from source channel ──
                        logger.warning(
                            f"[SEND] file_id stale for {f.get('file_name')} "
                            f"({err_str}) — trying forward fallback"
                        )
                        fallback_ok = await _forward_fallback(cyber, user_id, f, caption, auto_delay)
                        if not fallback_ok:
                            logger.error(
                                f"[SEND] Both file_id and forward failed for "
                                f"{f.get('file_name')} uid={user_id}"
                            )
                        return   # fallback handled auto_delay internally
                    else:
                        logger.error(f"SEND FAILED uid={user_id} file={f.get('file_name')}: {e}")
                        return
                except Exception as e:
                    logger.error(f"SEND FAILED uid={user_id} file={f.get('file_name')}: {e}")
                    return
            else:
                # No file_id at all — go straight to forward fallback
                logger.warning(
                    f"[SEND] No file_id for {f.get('file_name')} — using forward fallback directly"
                )
                fallback_ok = await _forward_fallback(cyber, user_id, f, caption, auto_delay)
                if not fallback_ok:
                    logger.error(
                        f"[SEND] Forward also failed for {f.get('file_name')} uid={user_id}"
                    )
                return

            # file_id send succeeded — schedule auto-delete if needed
            if sent_msg and auto_delay > 0:
                delete_message_later(cyber, user_id, sent_msg.id, auto_delay)

        await queue.put(send_job)

    # Clear session AFTER populating queue (not before)
    clear_session(user_id)

    if not is_sender_running(user_id):
        USER_SEND_TASKS[user_id] = asyncio.create_task(
            sender_worker(cyber, user_id, on_complete=on_complete)
        )
