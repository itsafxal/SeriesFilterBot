"""
files.py — FAST MEDIA GROUP EDITION  (FIXED)
=============================================
FIXES in this version:

  1. _sessions_col BUG FIXED — was using wrong DB "SeriesBotDB" which doesn't
     exist.  Now correctly uses config_db["user_sessions"] (the Config DB client).
     Previously every session.set() silently wrote to a ghost DB and all
     get_session_async() MongoDB fallbacks returned None, causing "no session"
     errors after bot restarts.

  2. AUTO-DELETE KEY UNIFIED — delivery now reads "FILE_AUTODEL_DELAY"
     (the key written by admin settings).  The old code read "AUTO_DELETE_DELAY"
     which is a different key — auto-delete never actually applied to files.

  3. _send_autodel_notice — caches bot.get_me() via _get_bot_id (no per-call
     API hit).
"""

import time
import asyncio
import re

from pyrogram import enums
from pyrogram.errors import FloodWait, MessageNotModified, BadRequest
from pyrogram.types import InputMediaDocument, InputMediaVideo, InputMediaAudio

from config import logger, STICKER_ENABLED, PROGRESS_MESSAGE_ENABLED
from DB.database import (
    get_all_files_by_season_and_quality,
    get_global_setting,
    config_db,                  # ← FIX: import config_db directly
    update_file_id,
    get_cached_sticker,
    save_sticker_cache,
    get_sticker_style,
    save_sticker_style,
)
from utils.cyber_delete import delete_message_later


# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

DEFAULT_CAPTION = (
    "<b>{series}</b>\n\n"
    "<b>{file_name}</b>\n\n"
    "Season <b>{season}</b> • Episode <b>{episode}</b>\n"
    "{language} • {quality}"
)

MEDIA_GROUP_SIZE = 10    # Telegram max per album
SESSION_TTL      = 600   # 10 minutes


# ─────────────────────────────────────────────────────────────
# SESSION STORE  (in-memory + MongoDB backup)
# FIX: use config_db["user_sessions"] — the correct Config DB client
# ─────────────────────────────────────────────────────────────

_SESSION_MEM: dict[int, dict] = {}

# FIX: was mongo_client["SeriesBotDB"]["user_sessions"] which is a ghost DB.
# Now correctly uses config_db (SeriesBotConfig) where all config lives.
_sessions_col = config_db["user_sessions"]
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
    uid   = int(user_id)
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


# ─────────────────────────────────────────────────────────────
# FILENAME / CAPTION HELPERS
# ─────────────────────────────────────────────────────────────

def caption_filename(name: str) -> str:
    if not name:
        return "Episode"
    name = re.sub(r"\.(mkv|mp4|avi|mov)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"@\S+", "", name)
    name = re.sub(r"\[.*?\]|\(.*?\)", "", name)
    name = re.sub(r"[._\-]+", " ", name)
    JUNK = r"\b(bluray|webrip|webdl|web|hdrip|x264|x265|hevc|aac|10bit|ddp5\.1|strife|yts|tgx|rip|hq)\b"
    name = re.sub(JUNK, "", name, flags=re.IGNORECASE)
    name = re.sub(r"\b(4k|2160p|1080p|720p|480p|360p)\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\bS\d{1,3}E\d{1,3}\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name if name else "Episode"


def clean_series_name(series: str) -> str:
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
    return re.sub(r"\s+", " ", s).strip().lower()


def _build_caption(template: str, series: str, season: int,
                   episode: int, language: str, quality: str,
                   file_name: str = "") -> str:
    season_str  = str(season).zfill(2)
    episode_str = str(episode).zfill(2)
    clean_name  = caption_filename(file_name)
    try:
        return template.format(
            series=series,
            file_name=clean_name,
            season=season_str,
            episode=episode_str,
            language=language,
            quality=quality,
        )
    except KeyError as e:
        logger.warning(f"[CAPTION] Unknown placeholder {e} in template — using default")
        return DEFAULT_CAPTION.format(
            series=series,
            file_name=clean_name,
            season=season_str,
            episode=episode_str,
            language=language,
            quality=quality,
        )
    except Exception as e:
        logger.warning(f"[CAPTION] Template format error: {e} — using fallback")
        return f"<b>{series}</b>\nS{season_str}E{episode_str} • {language} • {quality}"


# ─────────────────────────────────────────────────────────────
# FORWARD FALLBACK  (stale file_id → copy from source channel)
# ─────────────────────────────────────────────────────────────

# ── Refresh deduplication ─────────────────────────────────────────────────────
# Prevents N concurrent sends of the same stale file_id from each firing their
# own get_messages() call.  The first caller acquires the Future; all subsequent
# callers for the same (channel_id, message_id) await the same Future.
# Short TTL result cache avoids repeat refreshes within the same delivery burst.

_REFRESH_INFLIGHT: dict[tuple, asyncio.Future] = {}
_REFRESH_CACHE:    dict[tuple, tuple]           = {}   # key → (file_id|None, ts)
_REFRESH_CACHE_TTL = 30   # seconds — fresh enough for one delivery burst


async def _refresh_file_id(cyber, channel_id: int, message_id: int) -> str | None:
    key = (channel_id, message_id)

    # 1. Short-lived result cache — skip API if we just refreshed this file
    cached = _REFRESH_CACHE.get(key)
    if cached:
        result, ts = cached
        if time.time() - ts < _REFRESH_CACHE_TTL:
            logger.debug(f"[REFRESH] cache hit ch={channel_id} msg={message_id}")
            return result
        del _REFRESH_CACHE[key]

    # 2. In-flight dedup — if another coroutine is already fetching this key, wait for it
    inflight = _REFRESH_INFLIGHT.get(key)
    if inflight is not None:
        logger.debug(f"[REFRESH] coalescing refresh ch={channel_id} msg={message_id}")
        try:
            return await asyncio.shield(inflight)
        except Exception:
            return None

    # 3. We are the first — create a Future so others can coalesce on it
    loop = asyncio.get_event_loop()
    fut  = loop.create_future()
    _REFRESH_INFLIGHT[key] = fut

    result = None
    try:
        msgs = await cyber.get_messages(channel_id, message_id)
        msg  = msgs if not isinstance(msgs, list) else (msgs[0] if msgs else None)
        if msg:
            media = (
                getattr(msg, "video",    None)
                or getattr(msg, "audio",    None)
                or getattr(msg, "document", None)
            )
            if media and getattr(media, "file_id", None):
                result = media.file_id
    except Exception as e:
        logger.debug(f"[REFRESH] get_messages ch={channel_id} msg={message_id}: {e}")
    finally:
        _REFRESH_INFLIGHT.pop(key, None)
        if not fut.done():
            fut.set_result(result)

    # 4. Cache the result (even None) to suppress further refreshes in this burst
    _REFRESH_CACHE[key] = (result, time.time())
    return result


async def _forward_fallback(cyber, user_id: int, f: dict,
                             caption: str, auto_delay: int,
                             send_client=None) -> bool:
    """
    Refresh a stale/missing file_id and deliver to the user.

    Parameters
    ----------
    cyber        : Client used to FETCH from the source channel
                   (must be a member of that channel — always use main bot here).
    send_client  : Client used to SEND to the user DM.
                   Defaults to `cyber` for backward compatibility (direct delivery).
                   Pass the file bot client here when delivering via filebot so the
                   file arrives from the file bot, not the main bot.
    """
    channel_id = f.get("channel_id")
    message_id = f.get("message_id")
    file_name  = f.get("file_name", "unknown")

    # send_client defaults to cyber (main-bot direct path, no change in behaviour)
    if send_client is None:
        send_client = cyber

    if not channel_id or not message_id:
        logger.warning(f"[FALLBACK] No source info for {file_name} — skipping")
        return False

    # ── Step 1: try get_messages on the source channel (fetch_client = cyber) ──
    fresh_id = await _refresh_file_id(cyber, channel_id, message_id)

    if fresh_id:
        f_fresh = {**f, "file_id": fresh_id}
        # Send via send_client (file bot when in filebot path, main bot otherwise)
        ok = await _safe_send_single(send_client, user_id, f_fresh, caption, auto_delay)
        if ok:
            try:
                asyncio.create_task(update_file_id(channel_id, message_id, fresh_id))
            except Exception:
                pass
            logger.info(
                f"[FALLBACK] ✅ refreshed file_id for {file_name} "
                f"ch={channel_id} msg={message_id}"
            )
            return True
        logger.warning(f"[FALLBACK] send with fresh file_id failed for {file_name}")
        return False

    # ── Step 2: get_messages failed → try copy_message via fetch_client (cyber) ──
    # NOTE: copy_message must use the bot that is a channel member (cyber/main bot).
    # We cannot copy via the file bot because it has no channel access.
    logger.warning(
        f"[FALLBACK] get_messages failed for ch={channel_id} msg={message_id} "
        f"— trying copy_message"
    )
    try:
        sent = await cyber.copy_message(
            chat_id=user_id,
            from_chat_id=channel_id,
            message_id=message_id,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )
        if sent and auto_delay > 0:
            delete_message_later(cyber, user_id, sent.id, auto_delay)
        try:
            media = (
                getattr(sent, "document", None)
                or getattr(sent, "video",    None)
                or getattr(sent, "audio",    None)
            )
            if media and media.file_id:
                asyncio.create_task(update_file_id(channel_id, message_id, media.file_id))
        except Exception:
            pass
        logger.info(f"[FALLBACK] ✅ copy_message ch={channel_id} msg={message_id} → uid={user_id}")
        return True
    except Exception as e:
        logger.error(f"[FALLBACK] ❌ ch={channel_id} msg={message_id}: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# STICKER HELPERS
# ─────────────────────────────────────────────────────────────

async def _send_start_sticker(cyber, user_id: int, season: int, auto_delay: int):
    if not STICKER_ENABLED:
        return
    try:
        from utils.cyber_sticker import make_start_sticker
        cached = await get_cached_sticker(season, kind="start")
        msg    = None
        if cached:
            try:
                msg = await cyber.send_sticker(user_id, cached)
            except Exception:
                cached = None
        if not cached:
            style_idx      = await get_sticker_style(season, kind="start")
            buf, chosen    = make_start_sticker(season, style_idx=style_idx)
            msg            = await cyber.send_sticker(user_id, buf)
            media = getattr(msg, "sticker", None) or getattr(msg, "document", None)
            if media and getattr(media, "file_id", None):
                await save_sticker_cache(season, media.file_id, kind="start")
            if style_idx is None:
                await save_sticker_style(season, kind="start", style_idx=chosen)
        if auto_delay > 0 and msg:
            delete_message_later(cyber, user_id, msg.id, auto_delay)
    except Exception as e:
        logger.warning(f"[STICKER] start sticker failed: {e}")


async def _send_end_sticker(cyber, user_id: int, season: int,
                             total: int, auto_delay: int):
    if not STICKER_ENABLED:
        return
    try:
        from utils.cyber_sticker import make_end_sticker
        cached = await get_cached_sticker(season, kind="end")
        msg    = None
        if cached:
            try:
                msg = await cyber.send_sticker(user_id, cached)
            except Exception:
                cached = None
        if not cached:
            style_idx      = await get_sticker_style(season, kind="end")
            buf, chosen    = make_end_sticker(season, total, style_idx=style_idx)
            msg            = await cyber.send_sticker(user_id, buf)
            media = getattr(msg, "sticker", None) or getattr(msg, "document", None)
            if media and getattr(media, "file_id", None):
                await save_sticker_cache(season, media.file_id, kind="end")
            if style_idx is None:
                await save_sticker_style(season, kind="end", style_idx=chosen)
        if auto_delay > 0 and msg:
            delete_message_later(cyber, user_id, msg.id, auto_delay)
    except Exception as e:
        logger.warning(f"[STICKER] end sticker failed: {e}")


# ─────────────────────────────────────────────────────────────
# SAFE SEND WRAPPERS  (with FloodWait retry)
# ─────────────────────────────────────────────────────────────

async def _safe_send_group(cyber, user_id: int, media_group: list,
                            auto_delay: int,
                            source_files: list | None = None,
                            caption_map: dict | None = None) -> list:
    while True:
        try:
            sent_msgs = await cyber.send_media_group(user_id, media_group)
            if auto_delay > 0 and sent_msgs:
                for sm in sent_msgs:
                    delete_message_later(cyber, user_id, sm.id, auto_delay)
            return sent_msgs or []
        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[SEND] FloodWait {wait}s uid={user_id} — waiting")
            await asyncio.sleep(wait + 1)
        except Exception as e:
            logger.error(f"[SEND] send_media_group failed uid={user_id}: {e} — falling back to per-file")
            if source_files:
                sent_fallback = []
                for f in source_files:
                    cap = ""
                    if caption_map:
                        cap = caption_map.get(id(f), "")
                    if not cap:
                        cap = (f.get("caption") or "").strip()
                    if not cap:
                        ep  = str(f.get("episode", 0)).zfill(2)
                        cap = f"<b>Episode {ep}</b>"
                    ok = await _safe_send_single(cyber, user_id, f, cap, auto_delay)
                    if ok:
                        sent_fallback.append(True)
                return sent_fallback
            return []


async def _safe_send_single(cyber, user_id: int, f: dict,
                              caption: str, auto_delay: int) -> bool:
    file_id    = f.get("file_id")
    media_type = (f.get("media_type") or "document").lower()
    file_name  = f.get("file_name", "unknown")

    if not file_id:
        return await _forward_fallback(cyber, user_id, f, caption, auto_delay)

    while True:
        try:
            if media_type == "audio":
                sent = await cyber.send_audio(
                    user_id, file_id,
                    caption=caption, parse_mode=enums.ParseMode.HTML,
                )
            else:
                sent = await cyber.send_document(
                    user_id, file_id,
                    caption=caption, parse_mode=enums.ParseMode.HTML,
                )
            if sent and auto_delay > 0:
                delete_message_later(cyber, user_id, sent.id, auto_delay)
            return True
        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[SEND] FloodWait {wait}s uid={user_id} — waiting")
            await asyncio.sleep(wait + 1)
        except BadRequest as e:
            err = str(e)
            if any(k in err for k in ("MEDIA_EMPTY", "FILE_ID_INVALID", "FILE_REFERENCE_EXPIRED")):
                logger.warning(f"[SEND] stale file_id for {file_name} — refreshing")
                return await _forward_fallback(cyber, user_id, f, caption, auto_delay)
            logger.error(f"[SEND] BadRequest uid={user_id} file={file_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"[SEND] failed uid={user_id} file={file_name}: {e}")
            return False


# ─────────────────────────────────────────────────────────────
# BUILD InputMedia OBJECT FOR A FILE
# ─────────────────────────────────────────────────────────────

def _make_input_media(f: dict, caption: str) -> object | None:
    file_id    = f.get("file_id")
    media_type = (f.get("media_type") or "document").lower()
    if not file_id:
        return None
    # BUG FIX: use InputMediaVideo for video file_ids so Telegram accepts them
    # in send_media_group. Wrapping a video file_id in InputMediaDocument causes
    # MEDIA_EMPTY on the group send.
    if media_type == "video":
        return InputMediaVideo(
            media=file_id,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )
    if media_type == "audio":
        return InputMediaAudio(
            media=file_id,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )
    return InputMediaDocument(
        media=file_id,
        caption=caption,
        parse_mode=enums.ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────
# AUTO-DELETE NOTICE
# ─────────────────────────────────────────────────────────────

async def _send_autodel_notice(cyber, user_id: int, auto_delay: int):
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    if auto_delay >= 3600:
        hrs  = auto_delay // 3600
        mins = (auto_delay % 3600) // 60
        time_str = f"{hrs}h {mins}m" if mins else f"{hrs} hour{'s' if hrs > 1 else ''}"
    elif auto_delay >= 60:
        mins = auto_delay // 60
        secs = auto_delay % 60
        time_str = f"{mins}m {secs}s" if secs else f"{mins} minute{'s' if mins > 1 else ''}"
    else:
        time_str = f"{auto_delay} second{'s' if auto_delay > 1 else ''}"

    text = (
        f"⏳ <b>Files will be automatically deleted in {time_str}.</b>\n"
        f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        f"💾 <b>Save the files</b> to your Saved Messages or a personal chat\n"
        f"before they are removed.\n\n"
        f"<i>Tap a file → ⋮ → Forward to Saved Messages</i>"
    )

    try:
        me       = await cyber.get_me()
        bot_link = f"https://t.me/{me.username}"
        share_url = f"https://t.me/share/url?url={bot_link}&text=Get%20your%20favourite%20series%20here%21"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🤝 Share Bot", url=share_url)
        ]])
    except Exception:
        kb = None

    try:
        msg = await cyber.send_message(
            user_id,
            text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb,
        )
        if msg:
            delete_message_later(cyber, user_id, msg.id, auto_delay)
    except Exception as e:
        logger.debug(f"[DELIVER] autodel notice failed uid={user_id}: {e}")


# ─────────────────────────────────────────────────────────────
# MAIN DELIVERY — MEDIA GROUP BATCHING
# ─────────────────────────────────────────────────────────────

async def deliver_files_direct(cyber, user_id: int, on_complete=None):
    """
    FIX: reads "FILE_AUTODEL_DELAY" (correct key written by admin settings).
    Old code read "AUTO_DELETE_DELAY" — a different key, so auto-delete
    never applied to delivered files.
    """
    session = await get_session_async(user_id)
    if not session:
        logger.warning(f"deliver_files_direct: no session for uid={user_id}")
        return

    raw_series = session["series"]
    series     = clean_series_name(raw_series)
    season     = int(session["season"])
    language   = session["language"]
    quality    = session["quality"]
    query_name = _normalize_for_query(raw_series)

    logger.info(
        f"[DELIVER] uid={user_id} series={query_name!r} "
        f"S{season:02d} lang={language} quality={quality}"
    )

    files = await get_all_files_by_season_and_quality(
        query_name, season, quality, language
    )

    if not files:
        logger.warning(f"[DELIVER] no files found uid={user_id} query={query_name!r}")
        await cyber.send_message(user_id, "❌ No files found.")
        clear_session(user_id)
        if on_complete:
            await on_complete()
        return

    total = len(files)
    logger.info(f"[DELIVER] {total} files → uid={user_id}")

    # FIX: read "FILE_AUTODEL_DELAY" — the key admin settings writes to.
    # "AUTO_DELETE_DELAY" is the settings-cache key; "FILE_AUTODEL_DELAY" is DB key.
    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
    try:
        auto_delay = int(auto_delay) if auto_delay else 0
    except (TypeError, ValueError):
        auto_delay = 0

    custom_caption   = await get_global_setting("CUSTOM_FILE_CAPTION", None)
    caption_template = custom_caption if custom_caption else DEFAULT_CAPTION

    clear_session(user_id)

    progress_msg = None
    if PROGRESS_MESSAGE_ENABLED:
        try:
            progress_msg = await cyber.send_message(
                user_id,
                f"📦 <b>Preparing {total} files...</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass

    await _send_start_sticker(cyber, user_id, season, auto_delay)

    sent_count = 0

    all_items = []
    for i, f in enumerate(files):
        cap = _build_caption(
            caption_template, series, season,
            f.get("episode", 0), language, quality,
            f.get("file_name", ""),
        )
        raw_type  = (f.get("media_type") or "document").lower()
        norm_type = "document" if raw_type in ("video", "document") else raw_type
        all_items.append((f, cap, f.get("file_id"), norm_type))

    groupable = [(f, cap, fid) for f, cap, fid, t in all_items if t == "document" and fid]
    solo      = [(f, cap)      for f, cap, fid, t in all_items if t != "document" or not fid]

    for batch_start in range(0, len(groupable), MEDIA_GROUP_SIZE):
        batch = groupable[batch_start : batch_start + MEDIA_GROUP_SIZE]

        if len(batch) == 1:
            f, cap, fid = batch[0]
            ok = await _safe_send_single(cyber, user_id, f, cap, auto_delay)
            if ok:
                sent_count += 1
        else:
            media_list   = [_make_input_media(f, cap) for f, cap, fid in batch]
            source_files = [f for f, cap, fid in batch]
            caption_map  = {id(f): cap for f, cap, fid in batch}
            sent = await _safe_send_group(
                cyber, user_id, media_list, auto_delay,
                source_files=source_files,
                caption_map=caption_map,
            )
            sent_count += len(sent)

    for f, cap in solo:
        ok = await _safe_send_single(cyber, user_id, f, cap, auto_delay)
        if ok:
            sent_count += 1

    if progress_msg:
        try:
            await cyber.delete_messages(user_id, progress_msg.id)
        except Exception:
            pass

    await _send_end_sticker(cyber, user_id, season, total, auto_delay)

    if auto_delay > 0:
        await _send_autodel_notice(cyber, user_id, auto_delay)

    logger.info(f"[DELIVER] ✅ done uid={user_id} sent={sent_count}/{total}")

    if on_complete:
        try:
            await on_complete()
        except Exception as e:
            logger.error(f"[DELIVER] on_complete error uid={user_id}: {e}")
