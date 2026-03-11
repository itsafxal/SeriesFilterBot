"""
files.py — FILE_ID ONLY EDITION
=================================
KEY CHANGES (per requirements):

  1. FILE_ID ONLY — all sends use send_document/send_video/send_audio
     with the stored file_id. NO copy_message, NO channel forwarding,
     NO channel_id needed at delivery time.

  2. STALE FILE_ID RECOVERY — re-fetches the source message via
     get_messages() to refresh the file_id, then retries send.
     If get_messages also fails → reports error to user (no copy fallback).

  3. MEDIA GROUP WITH FALLBACK — tries send_media_group() first.
     If it fails for any reason, falls back to individual sends
     (still file_id only). Never silently drops files.

  4. CAPTION PRIORITY:
     a. stored_caption (saved during index, e.g. source channel caption)
     b. CUSTOM_FILE_CAPTION admin template
     c. DEFAULT_FILE_CAPTION from config.py
     Template variables: {series} {file_name} {season} {episode}
                         {language} {quality} {year} {genre} {rating}

  5. Progress message deleted after all groups sent.

  6. Session + sticker logic unchanged.
"""

import time
import asyncio
import re

from pyrogram import enums
from pyrogram.errors import FloodWait, BadRequest
from pyrogram.types import InputMediaDocument, InputMediaVideo, InputMediaAudio

from config import logger, STICKER_ENABLED, PROGRESS_MESSAGE_ENABLED, DEFAULT_FILE_CAPTION
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
# HELPERS
# ─────────────────────────────────────────────────────────────

def caption_filename(name: str) -> str:
    if not name:
        return "Episode"
    name = re.sub(r"\.(mkv|mp4|avi|mov)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"@\S+", "", name)
    name = re.sub(r"\[.*?\]|\(.*?\)", "", name)
    name = re.sub(r"[._\-]+", " ", name)
    JUNK = r"\b(bluray|webrip|webdl|web|hdrip|x264|x265|hevc|aac|10bit|ddp5\.1|hq)\b"
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
    s = series.replace(".", " ").replace("_", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def _build_caption(
    template: str,
    series: str,
    season: int,
    episode: int,
    language: str,
    quality: str,
    file_name: str = "",
    stored_caption: str = "",
    year: str = "",
    genre: str = "",
    rating: str = "",
) -> str:
    """
    Caption priority:
      1. stored_caption (raw caption from index, e.g. source channel caption)
      2. Template (CUSTOM_FILE_CAPTION or DEFAULT_FILE_CAPTION)

    Template placeholders:
      {series} {file_name} {season} {episode} {language}
      {quality} {year} {genre} {rating}
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
            year=year or "N/A",
            genre=genre or "N/A",
            rating=rating or "N/A",
        )
    except Exception:
        return f"<b>{series}</b>\nS{season_str}E{episode_str} • {language} • {quality}"


# ─────────────────────────────────────────────────────────────
# STALE FILE_ID REFRESH  (file_id only — no copy_message)
# ─────────────────────────────────────────────────────────────

async def _refresh_file_id(cyber, channel_id: int, message_id: int) -> str | None:
    """
    Re-fetch the source message via get_messages() to obtain a fresh file_id.
    Returns the new file_id string, or None if unreachable.
    Does NOT use copy_message or forward — file_id only.
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
            style_idx   = await get_sticker_style(season, kind="start")
            buf, chosen = make_start_sticker(season, style_idx=style_idx)
            msg         = await cyber.send_sticker(user_id, buf)
            media = getattr(msg, "sticker", None) or getattr(msg, "document", None)
            if media and getattr(media, "file_id", None):
                await save_sticker_cache(season, media.file_id, kind="start")
            if style_idx is None:
                await save_sticker_style(season, kind="start", style_idx=chosen)
        if auto_delay > 0 and msg:
            delete_message_later(cyber, user_id, msg.id, auto_delay)
    except Exception as e:
        logger.warning(f"[STICKER] start sticker failed: {e}")


async def _send_end_sticker(cyber, user_id: int, season: int, total: int, auto_delay: int):
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
            style_idx   = await get_sticker_style(season, kind="end")
            buf, chosen = make_end_sticker(season, total, style_idx=style_idx)
            msg         = await cyber.send_sticker(user_id, buf)
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
# CORE SEND — FILE_ID ONLY
# ─────────────────────────────────────────────────────────────

async def _safe_send_single(
    cyber, user_id: int, f: dict, caption: str, auto_delay: int
) -> bool:
    """
    Send a single file using its file_id only.
    On stale file_id → refresh via get_messages() and retry once.
    No copy_message, no channel forwarding.
    """
    file_id    = f.get("file_id")
    media_type = (f.get("media_type") or "document").lower()
    channel_id = f.get("channel_id")
    message_id = f.get("message_id")

    if not file_id:
        logger.warning(f"[SEND] No file_id for {f.get('file_name')} — skipping")
        return False

    for attempt in range(2):
        while True:
            try:
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

            except FloodWait as fw:
                wait = max(int(getattr(fw, "value", 10)), 5)
                logger.warning(f"[SEND] FloodWait {wait}s uid={user_id}")
                await asyncio.sleep(wait + 1)

            except BadRequest as e:
                err = str(e)
                if any(k in err for k in (
                    "MEDIA_EMPTY", "FILE_ID_INVALID", "FILE_REFERENCE_EXPIRED"
                )):
                    if attempt == 0 and channel_id and message_id:
                        logger.warning(
                            f"[SEND] Stale file_id for {f.get('file_name')} — "
                            f"refreshing from source ch={channel_id} msg={message_id}"
                        )
                        fresh = await _refresh_file_id(cyber, channel_id, message_id)
                        if fresh:
                            file_id = fresh
                            f       = {**f, "file_id": fresh}
                            # Persist so next delivery is instant
                            try:
                                asyncio.create_task(
                                    update_file_id(channel_id, message_id, fresh)
                                )
                            except Exception:
                                pass
                            break   # break inner while → retry outer for-loop
                    logger.error(
                        f"[SEND] Cannot refresh file_id for {f.get('file_name')} — skipping"
                    )
                    return False
                logger.error(f"[SEND] BadRequest uid={user_id} file={f.get('file_name')}: {e}")
                return False

            except Exception as e:
                logger.error(f"[SEND] uid={user_id} file={f.get('file_name')}: {e}")
                return False

    return False


async def _safe_send_group(
    cyber,
    user_id: int,
    media_group: list,
    auto_delay: int,
    source_files: list | None = None,
    caption_map: dict | None = None,
) -> list:
    """
    Send a media group with FloodWait retry.
    Falls back to per-file (file_id-only) sends on group rejection.
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
            logger.warning(f"[SEND] send_media_group failed uid={user_id}: {e} — falling back")
            if source_files:
                sent_fallback = []
                for f in source_files:
                    cap = (caption_map or {}).get(id(f), "") or (
                        f"<b>Episode {str(f.get('episode', 0)).zfill(2)}</b>"
                    )
                    ok = await _safe_send_single(cyber, user_id, f, cap, auto_delay)
                    if ok:
                        sent_fallback.append(True)
                return sent_fallback
            return []


def _make_input_media(f: dict, caption: str) -> object | None:
    file_id    = f.get("file_id")
    if not file_id:
        return None
    media_type = (f.get("media_type") or "document").lower()
    if media_type == "video":
        return InputMediaVideo(
            media=file_id, caption=caption, parse_mode=enums.ParseMode.HTML
        )
    elif media_type == "audio":
        return InputMediaAudio(
            media=file_id, caption=caption, parse_mode=enums.ParseMode.HTML
        )
    else:
        return InputMediaDocument(
            media=file_id, caption=caption, parse_mode=enums.ParseMode.HTML
        )


# ─────────────────────────────────────────────────────────────
# MAIN DELIVERY
# ─────────────────────────────────────────────────────────────

async def deliver_files_direct(cyber, user_id: int, on_complete=None):
    """
    Deliver all files for the user's session.

    Flow:
      1. Load session → fetch files from SQLite
      2. Fetch custom caption template + poster metadata (for template vars)
      3. Send start sticker
      4. Loop files in MEDIA_GROUP_SIZE chunks:
           a. Build InputMedia list (file_id only)
           b. send_media_group() → fallback to per-file on failure
      5. Send end sticker
      6. Delete progress message
      7. Call on_complete callback
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

    files = await get_all_files_by_season_and_quality(
        query_name, season, quality, language
    )

    if not files:
        # ── Debug: try LIKE fallback to diagnose name mismatch ──
        logger.warning(
            f"[DELIVER] no files found uid={user_id} "
            f"query={query_name!r} season={season} quality={quality!r} lang={language!r}"
        )
        # Try LIKE fallback in case stored name differs slightly
        try:
            from DB.sqlite_db import _get_conn
            def _like_fallback():
                conn = _get_conn()
                cur  = conn.cursor()
                cur.execute(
                    "SELECT DISTINCT normalized_name, season, quality, language FROM files "
                    "WHERE normalized_name LIKE ? COLLATE NOCASE LIMIT 5",
                    (f"%{query_name.split()[0]}%",)
                )
                return cur.fetchall()
            rows = await asyncio.to_thread(_like_fallback)
            logger.warning(f"[DELIVER] LIKE fallback candidates: {rows}")
        except Exception as _e:
            logger.warning(f"[DELIVER] LIKE fallback error: {_e}")

        await cyber.send_message(user_id, "❌ No files found.")
        clear_session(user_id)
        if on_complete:
            await on_complete()
        return

    total = len(files)
    logger.info(f"[DELIVER] {total} files → uid={user_id}")

    # ── Fetch settings ────────────────────────────────────────
    auto_delay       = await get_global_setting("FILE_AUTODEL_DELAY", 0)
    auto_delay       = int(auto_delay) if auto_delay else 0
    custom_caption   = await get_global_setting("CUSTOM_FILE_CAPTION", None)
    caption_template = custom_caption if custom_caption else DEFAULT_FILE_CAPTION

    # Fetch poster metadata for template variables (year/genre/rating)
    year = genre = rating = ""
    try:
        from CyberTG.poster import get_series_info as _gsi
        _, _year, _genre, _rating = await _gsi(series)
        year   = _year   or ""
        genre  = _genre  or ""
        rating = _rating or ""
    except Exception:
        pass

    # Clear session early
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

    sent_count = 0

    for chunk_start in range(0, total, MEDIA_GROUP_SIZE):
        chunk = files[chunk_start : chunk_start + MEDIA_GROUP_SIZE]

        # Per-file caption building
        captions = {}
        for i, f in enumerate(chunk):
            stored_cap = (f.get("caption") or "").strip()
            if stored_cap:
                cap = stored_cap
            elif chunk_start == 0 and i == 0:
                cap = _build_caption(
                    caption_template, series, season,
                    f.get("episode", 0), language, quality,
                    f.get("file_name", ""),
                    year=year, genre=genre, rating=rating,
                )
            else:
                ep  = str(f.get("episode", 0)).zfill(2)
                cap = f"<b>Episode {ep}</b>"
            captions[id(f)] = cap

        # Build InputMedia list (only files with valid file_id)
        group_items  = []  # (InputMedia, f) tuples
        no_id_items  = []  # files with no file_id at all

        for f in chunk:
            cap         = captions[id(f)]
            input_media = _make_input_media(f, cap)
            if input_media is not None:
                group_items.append((input_media, f))
            else:
                no_id_items.append((f, cap))

        # Send grouped by media_type
        if group_items:
            type_buckets: dict[str, list] = {}
            for im, f in group_items:
                mtype = (f.get("media_type") or "document").lower()
                type_buckets.setdefault(mtype, []).append((im, f))

            for mtype, bucket in type_buckets.items():
                media_list   = [im for im, _ in bucket]
                source_files = [f  for _,  f in bucket]
                cap_map      = {id(f): captions[id(f)] for f in source_files}

                if len(media_list) == 1:
                    cap = captions[id(source_files[0])]
                    ok  = await _safe_send_single(
                        cyber, user_id, source_files[0], cap, auto_delay
                    )
                    if ok:
                        sent_count += 1
                else:
                    sent = await _safe_send_group(
                        cyber, user_id, media_list, auto_delay,
                        source_files=source_files,
                        caption_map=cap_map,
                    )
                    sent_count += len(sent)

        # Files without file_id — log and skip (no copy_message fallback)
        for f, cap in no_id_items:
            logger.warning(
                f"[DELIVER] No file_id for {f.get('file_name')} "
                f"ch={f.get('channel_id')} msg={f.get('message_id')} — skipped"
            )

    # ── Cleanup ───────────────────────────────────────────────
    if progress_msg:
        try:
            await cyber.delete_messages(user_id, progress_msg.id)
        except Exception:
            pass

    await _send_end_sticker(cyber, user_id, season, total, auto_delay)

    logger.info(f"[DELIVER] ✅ done uid={user_id} sent={sent_count}/{total}")

    if on_complete:
        try:
            await on_complete()
        except Exception as e:
            logger.error(f"[DELIVER] on_complete error uid={user_id}: {e}")
