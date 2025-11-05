# commands.py

import asyncio
from pyrogram import Client, filters, enums
from config import ADMIN_USERS
from database import (
    get_bot_state,
    save_bot_state,
    get_global_setting,
    set_global_setting
)
from config import INDEX_CHANNELS, logger

# IMPORT HERE your save_series_file_for_bulk_index
from utils import save_series_file_for_bulk_index


def register_command_handlers(app: Client):

    @app.on_message(filters.command("start") & filters.private)
    async def start_cmd(_, m):
        await m.reply_text(
            "👋 Welcome!\n\nSend me your series name.\nI will show seasons → languages → qualities."
        )

    @app.on_message(filters.command("imdb") & filters.user(ADMIN_USERS) & filters.private)
    async def imdb_cmd(_, m):
        current = await get_global_setting("IMDB_POSTER_ENABLED", True)
        new_state = not current
        await set_global_setting("IMDB_POSTER_ENABLED", new_state)
        await m.reply_text(f"IMDB Poster: {'ON ✅' if new_state else 'OFF ❌'}")

    @app.on_message(filters.command("autodel") & filters.user(ADMIN_USERS) & filters.private)
    async def autodel_cmd(_, m):
        try:
            s = int(m.command[1])
        except:
            cur = await get_global_setting("AUTO_DELETE_TIMER", 0)
            return await m.reply_text(f"Current: {cur}s\nUsage: /autodel 300")

        await set_global_setting("AUTO_DELETE_TIMER", s)
        await m.reply_text(f"AUTO DELETE set → {s} seconds")

    @app.on_message(filters.command("index") & filters.user(ADMIN_USERS) & filters.private)
    async def index_cmd(app, m):
        await m.reply_text("Index starting…")

        total = 0
        for ch in INDEX_CHANNELS:
            last = await get_bot_state(ch) or 0

            async for msg in app.get_chat_history(ch, offset_id=last, reverse=True):
                if msg.document or msg.video or msg.audio:
                    saved, cnt = await save_series_file_for_bulk_index(msg)
                    if saved:
                        total += cnt
                        await save_bot_state(ch, msg.id)

        await m.reply_text(f"✅ Index Completed → {total} new files")

    return [
        start_cmd,
        imdb_cmd,
        autodel_cmd,
        index_cmd
    ]
