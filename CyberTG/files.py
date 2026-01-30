import time
import asyncio

from pyrogram import enums
from pyrogram.errors import FloodWait, MessageNotModified

from config import logger, CUSTOM_CAPTION_TEXT
from DB.database import get_all_files_by_season_and_quality


# =========================================================
# SESSION STORE
# =========================================================

SESSION_TTL = 600  # 10 minutes
USER_SESSIONS = {}


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

BASE_DELAY = 0.9
PROGRESS_EDIT_INTERVAL = 2


def get_user_queue(user_id):
    if user_id not in USER_SEND_QUEUES:
        USER_SEND_QUEUES[user_id] = asyncio.Queue()
    return USER_SEND_QUEUES[user_id]


def is_sender_running(user_id):
    t = USER_SEND_TASKS.get(user_id)
    return t and not t.done()


async def init_progress(cyber, user_id, series, total):
    msg = await cyber.send_message(
        user_id,
        f"📦 Sending files...\nSeries: {series}\nProgress: 0 / {total}",
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
    except:
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

    await init_progress(cyber, user_id, series, len(files))

    queue = get_user_queue(user_id)

    for f in files:

        async def send_job(f=f):
            caption = CUSTOM_CAPTION_TEXT.format(
                file_name=f.get("file_name", "Episode")
            )

            if f.get("file_id"):
                await cyber.send_document(
                    user_id,
                    f["file_id"],
                    caption=caption,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            else:
                await cyber.copy_message(
                    user_id,
                    f["channel_id"],
                    f["message_id"],
                    caption=caption,
                    parse_mode=enums.ParseMode.MARKDOWN
                )

        await queue.put(send_job)

    if not is_sender_running(user_id):
        USER_SEND_TASKS[user_id] = asyncio.create_task(
            sender_worker(cyber, user_id)
        )

    clear_session(user_id)
