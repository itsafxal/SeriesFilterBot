import time
import asyncio
import re

from pyrogram import enums
from pyrogram.errors import FloodWait, MessageNotModified

from config import logger
from DB.database import get_all_files_by_season_and_quality, get_global_setting
from utils.cyber_delete import delete_message_later


# =========================================================
# DEFAULT CAPTION (Fallback if DB caption not set)
# =========================================================

DEFAULT_CAPTION = """
<b>{series}</b>

<b>{file_name}</b>

Season <b>{season}</b> • Episode <b>{episode}</b>
{language} • {quality}
"""


# =========================================================
# SESSION STORE
# =========================================================

SESSION_TTL = 600  # 10 minutes
USER_SESSIONS = {}


def caption_filename(name: str):
    if not name:
        return "Episode"

    name = re.sub(r"\.(mkv|mp4|avi|mov)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[._\-]+", " ", name)

    JUNK = r"\b(bluray|webrip|webdl|hdrip|x264|x265|hevc|aac|10bit|ddp5\.1)\b"
    name = re.sub(JUNK, "", name, flags=re.IGNORECASE)

    name = re.sub(r"\b(4k|1080p|720p|480p|360p)\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()

    return name


def clean_series_name(series: str):
    if not series:
        return ""
    series = series.replace(".", " ").replace("_", " ")
    return re.sub(r"\s+", " ", series).strip()


def set_session(user_id: int, data: dict):
    USER_SESSIONS[user_id] = {
        "data": data,
        "expires": time.time() + SESSION_TTL
    }


def get_session(user_id: int):
    s = USER_SESSIONS.get(user_id)
    if not s:
        return None

    if time.time() > s["expires"]:
        USER_SESSIONS.pop(user_id, None)
        return None

    return s["data"]


def clear_session(user_id: int):
    USER_SESSIONS.pop(user_id, None)


# =========================================================
# SEND QUEUE + PROGRESS SYSTEM
# =========================================================

USER_SEND_QUEUES = {}
USER_SEND_TASKS = {}
USER_PROGRESS = {}

BASE_DELAY = 1.3
PROGRESS_EDIT_INTERVAL = 2


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
        "total": total,
        "sent": 0,
        "msg_id": msg.id,
        "last": time.time()
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
            f"📦 Sending files...<br>Progress: <b>{p['sent']}</b> / <b>{p['total']}</b>",
            parse_mode=enums.ParseMode.HTML
        )
        p["last"] = time.time()

    except MessageNotModified:
        pass


async def finish_progress(cyber, user_id):
    p = USER_PROGRESS.pop(user_id, None)
    if not p:
        return

    try:
        await cyber.edit_message_text(
            user_id,
            p["msg_id"],
            f"✅ <b>Delivery complete!</b><br>Sent: <b>{p['total']}</b> files",
            parse_mode=enums.ParseMode.HTML
        )

        delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
        if delay and delay > 0:
            delete_message_later(cyber, user_id, p["msg_id"], delay)

    except Exception:
        pass


async def sender_worker(cyber, user_id):
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
            await asyncio.sleep(fw.value + 1)

        except Exception as e:
            logger.error(f"SEND ERROR: {e}")
            await asyncio.sleep(1)

        finally:
            queue.task_done()

    await finish_progress(cyber, user_id)
    USER_SEND_TASKS.pop(user_id, None)


# =========================================================
# DELIVERY LOGIC
# =========================================================

async def deliver_files_direct(cyber, user_id):

    session = get_session(user_id)
    if not session:
        return

    raw_series = session["series"]
    series = clean_series_name(raw_series)
    season = session["season"]
    language = session["language"]
    quality = session["quality"]

    files = await get_all_files_by_season_and_quality(
        raw_series, season, quality, language
    )

    if not files:
        await cyber.send_message(user_id, "❌ No files found.")
        clear_session(user_id)
        return

    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)

    custom_caption = await get_global_setting(
        "CUSTOM_FILE_CAPTION",
        DEFAULT_CAPTION
    )

    await init_progress(cyber, user_id, len(files))

    queue = get_user_queue(user_id)

    for f in files:

        async def send_job(f=f):

            if not f.get("file_id"):
                return

            raw_name = f.get("file_name", "")
            clean_name = caption_filename(raw_name)

            episode = str(f.get("episode", 0)).zfill(2)
            season_str = str(season).zfill(2)

            caption = custom_caption.format(
                series=series,
                file_name=clean_name,
                season=season_str,
                episode=episode,
                language=language,
                quality=quality
            )

            sent_msg = None

            try:
                media_type = f.get("media_type")

                if media_type == "video":
                    sent_msg = await cyber.send_video(
                        user_id,
                        f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.HTML
                    )

                elif media_type == "audio":
                    sent_msg = await cyber.send_audio(
                        user_id,
                        f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.HTML
                    )

                else:
                    sent_msg = await cyber.send_document(
                        user_id,
                        f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.HTML
                    )

            except Exception as e:
                logger.error(f"SEND FAILED: {e}")
                return

            if sent_msg and auto_delay and auto_delay > 0:
                delete_message_later(
                    cyber,
                    user_id,
                    sent_msg.id,
                    auto_delay
                )

        await queue.put(send_job)

    if not is_sender_running(user_id):
        USER_SEND_TASKS[user_id] = asyncio.create_task(
            sender_worker(cyber, user_id)
        )

    clear_session(user_id)
