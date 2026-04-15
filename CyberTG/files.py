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
# MAIN DELIVERY
# ─────────────────────────────

async def deliver_files_direct(cyber, user_id):

    session = await get_session_async(user_id)
    if not session:
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
        return

    # ✅ SORT EPISODES PROPERLY
    files.sort(key=lambda x: int(x.get("episode", 0)))

    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
    auto_delay = int(auto_delay or 0)

    clear_session(user_id)

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
        await cyber.send_message(
            user_id,
            f"⏳ Files will delete in {auto_delay} seconds"
        )

    logger.info(f"✅ Delivered {sent}/{len(files)} to {user_id}")
