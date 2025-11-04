# index.py  — FULL FIXED VERSION
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import INDEX_CHANNELS, ADMIN_USERS, logger
from database import save_file_to_db, get_bot_state, save_bot_state
from utils import save_series_file_for_bulk_index



def register_media_handler(app: Client):

    # FIXED: order of filter MUST be channel first
    @app.on_message(filters.channel & (filters.document | filters.video | filters.audio))
    async def media_handler(_, message: Message):
        """
        Real-time indexing: every new post in index channels will get indexed.
        """
        if message.chat.id not in INDEX_CHANNELS:
            return
        
        saved, count = await save_series_file_for_bulk_index(message)

        if saved:
            logger.info(f"RTI: Indexed {count} new file(s) from channel {message.chat.id}")



def register_bulk_index_handlers(app: Client):

    @app.on_message(filters.command("index") & filters.user(ADMIN_USERS) & filters.private)
    async def index_command(_, message: Message):
        """
        bulk indexing — full scan
        """
        await message.reply_text("📦 Starting bulk indexing... Running full scan")

        total_indexed = 0

        for channel_id in INDEX_CHANNELS:

            await message.reply_text(f"⏳ Scanning `{channel_id}` ...", parse_mode=enums.ParseMode.MARKDOWN)

            last_indexed_id = await get_bot_state(channel_id)
            start_id = last_indexed_id if last_indexed_id else 0

            indexed_count = 0
            newest_processed_msg_id = start_id

            # MAIN FIX ✅ — reverse=True old → new ordering
            async for msg in app.get_chat_history(channel_id, reverse=True):

                # skip messages already processed
                if msg.id <= start_id:
                    continue

                if not msg.media or msg.media not in [
                    enums.MessageMediaType.DOCUMENT,
                    enums.MessageMediaType.VIDEO,
                    enums.MessageMediaType.AUDIO
                ]:
                    continue

                saved, count = await save_series_file_for_bulk_index(msg)

                if saved:
                    indexed_count += count
                    total_indexed += count
                    newest_processed_msg_id = msg.id

                # save state every 500 messages
                if indexed_count > 0 and indexed_count % 500 == 0:
                    await save_bot_state(channel_id, newest_processed_msg_id)
                    logger.debug(f"checkpoint saved at {newest_processed_msg_id}")
                    await asyncio.sleep(1)

            # save state final
            if newest_processed_msg_id > start_id:
                await save_bot_state(channel_id, newest_processed_msg_id)

            await message.reply_text(
                f"✅ Finished `{channel_id}` — added `{indexed_count}` files",
                parse_mode=enums.ParseMode.MARKDOWN
            )

        await message.reply_text(f"🎉 ALL DONE — Total newly indexed: **{total_indexed}**", parse_mode=enums.ParseMode.MARKDOWN)

    return [index_command]
