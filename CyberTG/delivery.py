from pyrogram import enums
from config import CUSTOM_CAPTION_TEXT
from DB.database import get_all_files_by_season_and_quality
from .session_store import get_session, clear_session
from .send_queue import (
    get_user_queue,
    is_sender_running,
    USER_SEND_TASKS,
    sender_worker,
    init_progress
)

async def deliver_files_direct(cyber, user_id: int):
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
