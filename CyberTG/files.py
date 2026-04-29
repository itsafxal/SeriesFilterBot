# files.py — SINGLE BOT CLEAN VERSION

import time
import asyncio
import re

from pyrogram import enums
from pyrogram.errors import FloodWait, BadRequest
from pyrogram.types import InputMediaDocument, InputMediaVideo, InputMediaAudio

from config import logger, STICKER_ENABLED, PROGRESS_MESSAGE_ENABLED
from DB.database import (
    get_all_files_by_season_and_quality,
    get_global_setting,
    config_db,
    update_file_id,
    get_cached_sticker,
    save_sticker_cache,
    get_sticker_style,
    save_sticker_style,
)
from utils.cyber_delete import delete_message_later


# ─────────────────────────────
# CONSTANTS
# ─────────────────────────────

DEFAULT_CAPTION = (
    "<b>{series}</b>\n\n"
    "<b>{file_name}</b>\n\n"
    "Season <b>{season}</b> • Episode <b>{episode}</b>\n"
    "{language} • {quality}"
)

MEDIA_GROUP_SIZE = 10
SESSION_TTL = 600


# ─────────────────────────────
# SESSION STORE
# ─────────────────────────────

_SESSION_MEM = {}
_sessions_col = config_db["user_sessions"]


def set_session(user_id, data):
    from datetime import datetime, timedelta

    expires = time.time() + SESSION_TTL
    _SESSION_MEM[user_id] = {"data": data, "expires": expires}

    doc = {
        "_id": user_id,
        "data": data,
        "expires_at": datetime.utcnow() + timedelta(seconds=SESSION_TTL),
    }

    asyncio.create_task(
        asyncio.to_thread(
            lambda: _sessions_col.replace_one({"_id": user_id}, doc, upsert=True)
        )
    )


async def get_session_async(user_id):
    from datetime import datetime

    entry = _SESSION_MEM.get(user_id)
    if entry and time.time() < entry["expires"]:
        return entry["data"]

    def _sync():
        doc = _sessions_col.find_one({"_id": user_id})
        if not doc:
            return None
        if doc["expires_at"] < datetime.utcnow():
            _sessions_col.delete_one({"_id": user_id})
            return None
        return doc["data"]

    return await asyncio.to_thread(_sync)


def clear_session(user_id):
    _SESSION_MEM.pop(user_id, None)
    asyncio.create_task(
        asyncio.to_thread(lambda: _sessions_col.delete_one({"_id": user_id}))
    )


# ─────────────────────────────
# HELPERS
# ─────────────────────────────

def caption_filename(name):
    name = re.sub(r"\.(mkv|mp4|avi)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[._\-]+", " ", name)
    return name.strip()


def build_caption(series, season, episode, language, quality, file_name):
    return DEFAULT_CAPTION.format(
        series=series,
        file_name=caption_filename(file_name),
        season=str(season).zfill(2),
        episode=str(episode).zfill(2),
        language=language,
        quality=quality,
    )


def make_media(f, caption):
    file_id = f["file_id"]
    t = f.get("media_type", "document")

    if t == "video":
        return InputMediaVideo(file_id, caption=caption, parse_mode=enums.ParseMode.HTML)
    if t == "audio":
        return InputMediaAudio(file_id, caption=caption, parse_mode=enums.ParseMode.HTML)
    return InputMediaDocument(file_id, caption=caption, parse_mode=enums.ParseMode.HTML)


# ─────────────────────────────
# SAFE SEND
# ─────────────────────────────

async def safe_send(cyber, user_id, f, caption, auto_delay):
    while True:
        try:
            sent = await cyber.send_document(
                user_id,
                f["file_id"],
                caption=caption,
                parse_mode=enums.ParseMode.HTML,
            )
            if auto_delay:
                delete_message_later(cyber, user_id, sent.id, auto_delay)
            return True

        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)

        except BadRequest:
            return False

        except Exception as e:
            logger.error(e)
            return False


# ─────────────────────────────
# STICKER HELPERS
# ─────────────────────────────

async def _send_start_sticker(cyber, user_id: int, season: int, auto_delay: int) -> int | None:
    """
    Send the START sticker for a season.
    Tries DB-cached file_id first; falls back to generating + uploading.
    Returns the sent message ID (so it can be scheduled for deletion), or None.
    FIX: style is persisted per season so the same style is reused on retries.
    """
    try:
        from utils.cyber_sticker import make_start_sticker

        # Reuse previously chosen style for this season (consistent appearance)
        style_idx = await get_sticker_style(season, "start")
        buf, chosen_style = make_start_sticker(season, style_idx=style_idx)

        # Persist style choice for future deliveries of this season
        if style_idx is None:
            await save_sticker_style(season, "start", chosen_style)

        sent = await cyber.send_sticker(user_id, buf)
        # Cache the file_id so next time we skip re-generation
        await save_sticker_cache(season, sent.sticker.file_id, kind="start")

        if auto_delay:
            delete_message_later(cyber, user_id, sent.id, auto_delay)

        return sent.id

    except Exception as e:
        logger.warning(f"[STICKER] Failed to send START sticker season={season}: {e}")
        return None


async def _send_end_sticker(cyber, user_id: int, season: int, total: int, auto_delay: int) -> int | None:
    """
    Send the END sticker after all files for a season are delivered.
    FIX: scheduled for deletion with same auto_delay as the files.
    """
    try:
        from utils.cyber_sticker import make_end_sticker

        style_idx = await get_sticker_style(season, "end")
        buf, chosen_style = make_end_sticker(season, total, style_idx=style_idx)

        if style_idx is None:
            await save_sticker_style(season, "end", chosen_style)

        sent = await cyber.send_sticker(user_id, buf)
        await save_sticker_cache(season, sent.sticker.file_id, kind="end")

        if auto_delay:
            delete_message_later(cyber, user_id, sent.id, auto_delay)

        return sent.id

    except Exception as e:
        logger.warning(f"[STICKER] Failed to send END sticker season={season}: {e}")
        return None


# ─────────────────────────────
# MAIN DELIVERY
# ─────────────────────────────

async def deliver_files_direct(cyber, user_id, on_complete=None):
    """
    Deliver files to user.
    on_complete: optional async callable invoked after delivery finishes
                 (used by fsub.py to release the delivery lock).
    """

    session = await get_session_async(user_id)
    if not session:
        if on_complete:
            await on_complete()
        return

    series = session["series"]
    season = int(session["season"])
    language = session["language"]
    quality = session["quality"]

    files = await get_all_files_by_season_and_quality(
        series.lower(), season, quality, language
    )

    if not files:
        await cyber.send_message(user_id, "❌ No files found.")
        clear_session(user_id)
        if on_complete:
            await on_complete()
        return

    # ✅ SORT EPISODES PROPERLY
    files.sort(key=lambda x: int(x.get("episode", 0)))

    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
    auto_delay = int(auto_delay or 0)

    clear_session(user_id)

    # ── SEND START STICKER ─────────────────────────────────────
    # FIX: sent before files; scheduled for deletion with same delay as files
    if STICKER_ENABLED:
        await _send_start_sticker(cyber, user_id, season, auto_delay)

    if PROGRESS_MESSAGE_ENABLED:
        msg = await cyber.send_message(user_id, f"📦 Sending {len(files)} files...")

    sent = 0

    # ───── SEND IN BATCHES ─────
    for i in range(0, len(files), MEDIA_GROUP_SIZE):
        batch = files[i:i + MEDIA_GROUP_SIZE]

        if len(batch) == 1:
            f = batch[0]
            cap = build_caption(series, season, f["episode"], language, quality, f["file_name"])
            if await safe_send(cyber, user_id, f, cap, auto_delay):
                sent += 1
        else:
            media = []
            for f in batch:
                cap = build_caption(series, season, f["episode"], language, quality, f["file_name"])
                media.append(make_media(f, cap))

            try:
                msgs = await cyber.send_media_group(user_id, media)
                sent += len(msgs)

                if auto_delay:
                    for m in msgs:
                        delete_message_later(cyber, user_id, m.id, auto_delay)

            except Exception as e:
                logger.error(f"Group failed → fallback {e}")
                for f in batch:
                    cap = build_caption(series, season, f["episode"], language, quality, f["file_name"])
                    if await safe_send(cyber, user_id, f, cap, auto_delay):
                        sent += 1

    if PROGRESS_MESSAGE_ENABLED:
        await cyber.delete_messages(user_id, msg.id)

    if auto_delay:
        notif = await cyber.send_message(
            user_id,
            f"⏳ Files will delete in {auto_delay} seconds"
        )
        delete_message_later(cyber, user_id, notif.id, auto_delay)

    # ── SEND END STICKER ──────────────────────────────────────
    # FIX: sent after all files, deleted with same auto_delay so it disappears
    # together with the files (style saved per-season to DB for consistency).
    if STICKER_ENABLED:
        await _send_end_sticker(cyber, user_id, season, sent, auto_delay)

    logger.info(f"✅ Delivered {sent}/{len(files)} to {user_id}")

    if on_complete:
        await on_complete()
