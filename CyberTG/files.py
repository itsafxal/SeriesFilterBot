import time
import asyncio
import re

from pyrogram import enums
from pyrogram.errors import FloodWait, MessageNotModified

from config import logger, CUSTOM_CAPTION_TEXT
from DB.database import get_all_files_by_season_and_quality, get_global_setting
from utils.cyber_delete import delete_message_later


# =========================================================
# SESSION STORE
# =========================================================

SESSION_TTL = 600  # 10 minutes
USER_SESSIONS = {}


def caption_filename(name: str):
    if not name:
        return "Episode"

    # Remove extension
    name = re.sub(r"\.(mkv|mp4|avi)$", "", name, flags=re.IGNORECASE)

    # Replace separators with space
    name = re.sub(r"[._\-]+", " ", name)

    # Remove unwanted junk words
    JUNK = r"\b(bluray|webrip|webdl|hdrip|x264|x265|hevc|aac|10bit|ddp5\.1)\b"
    name = re.sub(JUNK, "", name, flags=re.IGNORECASE)

    # Remove quality duplication if exists
    name = re.sub(r"\b(4k|1080p|720p|480p|360p)\b", "", name, flags=re.IGNORECASE)

    # Collapse spaces
    name = re.sub(r"\s+", " ", name).strip()

    return name


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
        parse_mode=enums.ParseMode.MARKDOWN
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
            f"📦 Sending files...\nProgress: {p['sent']} / {p['total']}",
            parse_mode=enums.ParseMode.MARKDOWN
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
            f"✅ Delivery complete!\nSent: {p['total']} files",
            parse_mode=enums.ParseMode.MARKDOWN
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

    series = session["series"]
    season = session["season"]
    language = session["language"]
    quality = session["quality"]

    files = await get_all_files_by_season_and_quality(
        series, season, quality, language
    )

    if not files:
        await cyber.send_message(user_id, "❌ No files found.")
        clear_session(user_id)
        return

    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)

    await init_progress(cyber, user_id, len(files))

    queue = get_user_queue(user_id)

    for f in files:

        async def send_job(f=f):

            raw_name = f.get("file_name", "")
            clean_name = caption_filename(raw_name)

            caption = CUSTOM_CAPTION_TEXT.format(
                file_name=clean_name,
                season=str(season).zfill(2),
                episode=str(f.get("episode", 0)).zfill(2),
                language=language,
                quality=quality
            )

            if not f.get("file_id"):
                return

            media_type = f.get("media_type")
            sent_msg = None

            try:
                if media_type == "video":
                    sent_msg = await cyber.send_video(
                        user_id,
                        f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )

                elif media_type == "audio":
                    sent_msg = await cyber.send_audio(
                        user_id,
                        f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )

                else:
                    sent_msg = await cyber.send_document(
                        user_id,
                        f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )

            except Exception:
                try:
                    sent_msg = await cyber.send_video(
                        user_id,
                        f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                except Exception:
                    sent_msg = await cyber.send_document(
                        user_id,
                        f["file_id"],
                        caption=caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )

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
