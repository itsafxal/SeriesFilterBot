"""
files.py — FAST MEDIA GROUP EDITION
======================================
Key changes vs old version:

  1. MEDIA GROUP BATCHING — files sent in groups of up to 10 using
     send_media_group(). Telegram renders these as a single album (like
     the screenshot). 10-file album = 1 API call instead of 10.

  2. ZERO BASE_DELAY — removed the old 1.3s sleep between every file.
     Groups are sent back-to-back. FloodWait is handled reactively
     (sleep only when Telegram actually complains).

  3. CONCURRENT FALLBACKS — when a file_id is stale the forward fallback
     runs immediately inside the group builder, not as a queued job.

  4. SIMPLE LINEAR SEND — no queue/worker/task machinery needed when
     sending groups. Delivery is a single async loop: build group → send →
     next group. Much simpler and faster.

  5. CAPTION on first file only — Telegram only shows one caption per
     album. First file gets the full caption; others get a minimal label.

  6. PROGRESS deleted after all groups sent (not per-file).

  7. Session + sticker logic unchanged — fully compatible.
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
    mongo_client,
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
# ─────────────────────────────────────────────────────────────

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
                   file_name: str = "", stored_caption: str = "") -> str:
    """
    Caption priority:
      1. stored_caption — raw caption saved during indexing (actual source channel caption)
      2. CUSTOM_FILE_CAPTION template (set via admin panel)
      3. DEFAULT_CAPTION template (hardcoded fallback)
    """
    if stored_caption and stored_caption.strip():
        return stored_caption.strip()

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
    except Exception:
        return f"<b>{series}</b>\nS{season_str}E{episode_str} • {language} • {quality}"


# ─────────────────────────────────────────────────────────────
# FORWARD FALLBACK  (stale file_id → copy from source channel)
# ─────────────────────────────────────────────────────────────

async def _refresh_file_id(cyber, channel_id: int, message_id: int) -> str | None:
    """
    Re-fetch the source message to get a fresh file_id.
    Used when the stored file_id has expired (Telegram invalidates them periodically).
    Returns the new file_id string, or None if the message is unreachable.
    """
    try:
        msgs = await cyber.get_messages(channel_id, message_id)
        msg  = msgs if not isinstance(msgs, list) else (msgs[0] if msgs else None)
        if not msg:
            return None
        media = (
            getattr(msg, "video",    None)
            or getattr(msg, "audio",    None)
            or getattr(msg, "document", None)
        )
        if media and getattr(media, "file_id", None):
            return media.file_id
    except Exception as e:
        logger.debug(f"[REFRESH] get_messages ch={channel_id} msg={message_id}: {e}")
    return None


async def _forward_fallback(cyber, user_id: int, f: dict,
                             caption: str, auto_delay: int) -> bool:
    """
    Stale file_id recovery:
      1. Re-fetch the source message to obtain a fresh file_id.
      2. Send directly to user using that fresh file_id (no copy/forward).
      3. Persist the new file_id to DB so future deliveries are instant.
    Falls back to copy_message only if get_messages itself fails.
    """
    channel_id = f.get("channel_id")
    message_id = f.get("message_id")
    file_name  = f.get("file_name", "unknown")

    if not channel_id or not message_id:
        logger.warning(f"[FALLBACK] No source info for {file_name} — skipping")
        return False

    # ── Step 1: refresh file_id via get_messages ──────────────
    fresh_id = await _refresh_file_id(cyber, channel_id, message_id)

    if fresh_id:
        # ── Step 2: send directly with fresh file_id ──────────
        f_fresh = {**f, "file_id": fresh_id}
        ok = await _safe_send_single(cyber, user_id, f_fresh, caption, auto_delay)
        if ok:
            # ── Step 3: persist new file_id so next delivery is instant ──
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

    # ── Step 4: get_messages failed — try copy_message as last resort ──
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
                            source_files: list | None = None) -> list:
    """Send a media group with FloodWait retry. Returns sent messages.
    source_files: original file dicts parallel to media_group — used to
    fall back to per-file sending when the group itself is rejected.
    """
    while True:
        try:
            sent_msgs = await cyber.send_media_group(user_id, media_group)
            if auto_delay > 0 and sent_msgs:
                for sm in sent_msgs:
                    delete_message_later(cyber, user_id, sm.id, auto_delay)
            return sent_msgs or []
        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[SEND] FloodWait {wait}s uid={user_id}")
            await asyncio.sleep(wait + 1)
        except Exception as e:
            err = str(e)
            logger.error(f"[SEND] send_media_group failed uid={user_id}: {e}")
            # MEDIA_EMPTY / stale file_ids — fall back to per-file send
            # so nothing is silently dropped.
            if source_files and any(k in err for k in (
                "MEDIA_EMPTY", "FILE_ID_INVALID", "FILE_REFERENCE_EXPIRED", "MEDIA_INVALID"
            )):
                logger.warning(
                    f"[SEND] Falling back to per-file send for {len(source_files)} files"
                )
                sent_fallback = []
                for f in source_files:
                    cap = (f.get("caption") or "").strip() or f"<b>Episode {str(f.get('episode', 0)).zfill(2)}</b>"
                    ok = await _safe_send_single(cyber, user_id, f, cap, auto_delay)
                    if ok:
                        sent_fallback.append(True)
                return sent_fallback
            return []


async def _safe_send_single(cyber, user_id: int, f: dict,
                              caption: str, auto_delay: int) -> bool:
    """Send a single file (fallback for files that can't be in a group)."""
    file_id    = f.get("file_id")
    media_type = f.get("media_type", "document")
    while True:
        try:
            if file_id:
                if media_type == "video":
                    sent = await cyber.send_video(
                        user_id, file_id,
                        caption=caption, parse_mode=enums.ParseMode.HTML,
                    )
                elif media_type == "audio":
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
            else:
                return await _forward_fallback(cyber, user_id, f, caption, auto_delay)
        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[SEND] FloodWait {wait}s uid={user_id}")
            await asyncio.sleep(wait + 1)
        except BadRequest as e:
            err = str(e)
            if any(k in err for k in ("MEDIA_EMPTY", "FILE_ID_INVALID", "FILE_REFERENCE_EXPIRED")):
                logger.warning(f"[SEND] stale file_id for {f.get('file_name')} — trying fallback")
                return await _forward_fallback(cyber, user_id, f, caption, auto_delay)
            logger.error(f"[SEND] BadRequest uid={user_id} file={f.get('file_name')}: {e}")
            return False
        except Exception as e:
            logger.error(f"[SEND] failed uid={user_id} file={f.get('file_name')}: {e}")
            return False


# ─────────────────────────────────────────────────────────────
# BUILD InputMedia OBJECT FOR A FILE
# ─────────────────────────────────────────────────────────────

def _make_input_media(f: dict, caption: str) -> object | None:
    """
    Build a Pyrogram InputMedia* object from a file dict.
    Returns None if no file_id (those files go to forward fallback instead).
    Caption is only set on the first file of each group (Telegram shows
    one caption per album).
    """
    file_id    = f.get("file_id")
    if not file_id:
        return None
    media_type = (f.get("media_type") or "document").lower()
    if media_type == "video":
        return InputMediaVideo(media=file_id, caption=caption,
                               parse_mode=enums.ParseMode.HTML)
    elif media_type == "audio":
        return InputMediaAudio(media=file_id, caption=caption,
                               parse_mode=enums.ParseMode.HTML)
    else:
        return InputMediaDocument(media=file_id, caption=caption,
                                  parse_mode=enums.ParseMode.HTML)


# ─────────────────────────────────────────────────────────────
# MAIN DELIVERY — MEDIA GROUP BATCHING
# ─────────────────────────────────────────────────────────────

async def deliver_files_direct(cyber, user_id: int, on_complete=None):
    """
    Deliver all files as media group albums (up to 10 per group).

    Flow:
      1. Load session → fetch files from SQLite
      2. Send start sticker
      3. Loop files in chunks of MEDIA_GROUP_SIZE:
           a. Build InputMedia list (skip files with no file_id → fallback)
           b. send_media_group() — 1 API call per 10 files
           c. Files with stale/no file_id → _forward_fallback (copy_message)
      4. Send end sticker
      5. Delete progress message
      6. Call on_complete callback
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

    # Fetch settings once
    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
    try:
        auto_delay = int(auto_delay) if auto_delay else 0
    except (TypeError, ValueError):
        auto_delay = 0

    custom_caption   = await get_global_setting("CUSTOM_FILE_CAPTION", None)
    caption_template = custom_caption if custom_caption else DEFAULT_CAPTION

    # Clear session early — files are already loaded
    clear_session(user_id)

    # Progress message
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

    # Start sticker
    await _send_start_sticker(cyber, user_id, season, auto_delay)

    # ── Send files in media groups ────────────────────────────
    sent_count = 0

    for chunk_start in range(0, total, MEDIA_GROUP_SIZE):
        chunk = files[chunk_start : chunk_start + MEDIA_GROUP_SIZE]

        # Separate files that can go in group vs those needing fallback
        group_items  = []   # (InputMedia, f) tuples for the group
        fallback_items = [] # files with no file_id → copy_message

        for i, f in enumerate(chunk):
            # Caption: full caption on first file of the first chunk,
            # minimal episode label on the rest
            stored_cap = (f.get("caption") or "").strip()
            if stored_cap:
                # Use the caption stored from the source channel directly
                cap = stored_cap
            elif chunk_start == 0 and i == 0:
                cap = _build_caption(
                    caption_template, series, season,
                    f.get("episode", 0), language, quality,
                    f.get("file_name", ""),
                )
            else:
                ep  = str(f.get("episode", 0)).zfill(2)
                cap = f"<b>Episode {ep}</b>"

            input_media = _make_input_media(f, cap)
            if input_media is not None:
                group_items.append((input_media, f))
            else:
                fallback_items.append((f, cap))

        # Send the group (if any files have valid file_ids)
        # FIX 1: Split by media_type — Telegram rejects mixed-type groups.
        # FIX 2: Pass source_files so _safe_send_group can fall back per-file on failure.
        if group_items:
            type_buckets: dict[str, list] = {}
            for im, f in group_items:
                mtype = (f.get("media_type") or "document").lower()
                type_buckets.setdefault(mtype, []).append((im, f))

            for mtype, bucket in type_buckets.items():
                media_list   = [im for im, _ in bucket]
                source_files = [f  for _,  f in bucket]
                if len(media_list) == 1:
                    cap = media_list[0].caption or ""
                    await _safe_send_single(cyber, user_id, source_files[0], cap, auto_delay)
                    sent_count += 1
                else:
                    sent = await _safe_send_group(
                        cyber, user_id, media_list, auto_delay,
                        source_files=source_files
                    )
                    sent_count += len(sent)

        # Send fallback files (no file_id) individually
        for f, cap in fallback_items:
            ok = await _forward_fallback(cyber, user_id, f, cap, auto_delay)
            if ok:
                sent_count += 1

    # ── Cleanup ───────────────────────────────────────────────

    # Delete progress message
    if progress_msg:
        try:
            await cyber.delete_messages(user_id, progress_msg.id)
        except Exception:
            pass

    # End sticker
    await _send_end_sticker(cyber, user_id, season, total, auto_delay)

    logger.info(f"[DELIVER] ✅ done uid={user_id} sent={sent_count}/{total}")

    if on_complete:
        try:
            await on_complete()
        except Exception as e:
            logger.error(f"[DELIVER] on_complete error uid={user_id}: {e}")
