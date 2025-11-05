# index.py
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import INDEX_CHANNELS, ADMIN_USERS, logger
from database import save_bot_state, get_bot_state
from utils import save_series_file_for_bulk_index


def register_media_handler(app: Client):

    # FIX: Correct filter precedence using parentheses
    @app.on_message((filters.document | filters.video | filters.audio) & filters.channel)
    async def media_handler(_, message: Message):
        if message.chat.id in INDEX_CHANNELS and message.media:

            saved, count = await save_series_file_for_bulk_index(message)

            if saved:
                logger.info(f"RTI: Indexed {count} new file(s) from channel {message.chat.id}")


def register_bulk_index_handlers(app: Client):

    @app.on_message(filters.command("index") & filters.user(ADMIN_USERS) & filters.private)
    async def index_command(_, message: Message):

        await message.reply_text("Starting bulk indexing. This may take a while...")

        total_indexed = 0

        for channel_id in INDEX_CHANNELS:

            last_indexed_id = await get_bot_state(channel_id)
            start_id = last_indexed_id if last_indexed_id else 0

            await message.reply_text(
                f"Processing channel `{channel_id}` from message ID `{start_id + 1}`...",
                parse_mode=enums.ParseMode.MARKDOWN
            )

            indexed_count = 0
            current_id = 0

            try:
                # *** IMPORTANT FIX ***
                async for msg in app.get_chat_history(
                        channel_id,
                        offset_id=start_id,
                        reverse=True):     # this fixes indexing stop bug

                    current_id = msg.id

                    if not (msg.document or msg.video or msg.audio):
                        continue

                    saved, count = await save_series_file_for_bulk_index(msg)
                    if saved:
                        indexed_count += count
                        total_indexed += count

                    if indexed_count > 0 and indexed_count % 500 == 0:
                        await save_bot_state(channel_id, current_id)
                        await asyncio.sleep(1)
                        logger.debug(f"INDEX: Checkpoint saved for {channel_id} at ID {current_id}")

                if current_id > start_id:
                    await save_bot_state(channel_id, current_id)

                await message.reply_text(
                    f"✅ Finished indexing `{channel_id}`. Indexed {indexed_count} new files.",
                    parse_mode=enums.ParseMode.MARKDOWN
                )

            except Exception as e:
                logger.error(f"Error indexing channel {channel_id}: {e}")
                await message.reply_text(
                    f"❌ Indexing failed for channel `{channel_id}`: `{e}`",
                    parse_mode=enums.ParseMode.MARKDOWN
                )

        await message.reply_text(
            f"🥳 **BULK INDEXING COMPLETE.** Total new files indexed: **{total_indexed}**.",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    return [index_command]
