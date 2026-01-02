import asyncio
import time
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram import enums
from config import logger

# ===============================
# QUEUES & TASKS
# ===============================
USER_SEND_QUEUES = {}     # user_id -> asyncio.Queue
USER_SEND_TASKS = {}     # user_id -> asyncio.Task
USER_PROGRESS = {}       # progress state

BASE_DELAY = 0.8
MAX_DELAY = 6.0
PROGRESS_EDIT_INTERVAL = 2.0

# ===============================
# QUEUE HELPERS
# ===============================
def get_user_queue(user_id: int) -> asyncio.Queue:
    if user_id not in USER_SEND_QUEUES:
        USER_SEND_QUEUES[user_id] = asyncio.Queue()
    return USER_SEND_QUEUES[user_id]

def is_sender_running(user_id: int) -> bool:
    t = USER_SEND_TASKS.get(user_id)
    return t and not t.done()

# ===============================
# PROGRESS UI
# ===============================
async def init_progress(cyber, user_id: int, series: str, total: int):
    msg = await cyber.send_message(
        user_id,
        f"📦 **Sending files…**\n"
        f"**Series:** {series}\n"
        f"**Progress:** 0 / {total}",
        parse_mode=enums.ParseMode.MARKDOWN
    )

    USER_PROGRESS[user_id] = {
        "total": total,
        "sent": 0,
        "msg_id": msg.id,
        "last_edit": time.time()
    }

async def update_progress(cyber, user_id: int):
    p = USER_PROGRESS.get(user_id)
    if not p:
        return

    now = time.time()
    if now - p["last_edit"] < PROGRESS_EDIT_INTERVAL:
        return

    try:
        await cyber.edit_message_text(
            chat_id=user_id,
            message_id=p["msg_id"],
            text=(
                f"📦 **Sending files…**\n"
                f"**Progress:** {p['sent']} / {p['total']}"
            ),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        p["last_edit"] = now
    except MessageNotModified:
        pass
    except Exception:
        pass

async def finish_progress(cyber, user_id: int):
    p = USER_PROGRESS.pop(user_id, None)
    if not p:
        return

    try:
        await cyber.edit_message_text(
            chat_id=user_id,
            message_id=p["msg_id"],
            text=(
                f"✅ **Delivery complete!**\n"
                f"**Sent:** {p['total']} files"
            ),
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception:
        pass

# ===============================
# SENDER WORKER (FLOODWAIT SAFE)
# ===============================
async def sender_worker(cyber, user_id: int):
    queue = get_user_queue(user_id)
    delay = BASE_DELAY

    while not queue.empty():
        job = await queue.get()

        try:
            await job()

            p = USER_PROGRESS.get(user_id)
            if p:
                p["sent"] += 1
                await update_progress(cyber, user_id)

            delay = BASE_DELAY
            await asyncio.sleep(delay)

        except FloodWait as fw:
            delay = min(fw.value + 0.5, MAX_DELAY)
            logger.warning(f"[FLOODWAIT] user={user_id} wait={fw.value}s")
            await asyncio.sleep(delay)

        except Exception as e:
            logger.error(f"[SEND ERROR] {e}")
            await asyncio.sleep(1.5)

        finally:
            queue.task_done()

    await finish_progress(cyber, user_id)
    USER_SEND_TASKS.pop(user_id, None)
