import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import INDEX_CHANNELS, ADMIN_USERS, logger
from database import save_file_to_db, get_bot_state, save_bot_state
from utils import save_series_file_for_bulk_index

def register_media_handler(app: Client):

    # FIX: Corrected filter precedence using parentheses.
    # The handler now correctly targets (Document OR Video OR Audio) AND (in a Channel).
    @app.on_message((filters.document | filters.video | filters.audio) & filters.channel)
    async def media_handler(_, message: Message):
        """
        Handles single file posts in indexed channels.
        This is primarily for real-time indexing of new posts (RTI).
        """
        if message.chat.id in INDEX_CHANNELS and message.media:
            # save_series_file_for_bulk_index handles analysis and saving.
            saved, count = await save_series_file_for_bulk_index(message)

            if saved:
                # The log for Real-Time Indexing (RTI)
                logger.info(f"RTI: Indexed {count} new file(s) from channel {message.chat.id}")


def register_bulk_index_handlers(app: Client):

    @app.on_message(filters.command("index") & filters.user(ADMIN_USERS) & filters.private)
    async def index_command(_, message: Message):
        """Starts the bulk indexing process for all configured channels."""
        await message.reply_text("Starting bulk indexing. This may take a while...")

        total_indexed = 0

        for channel_id in INDEX_CHANNELS:

            # 1. Get starting message ID from DB (to resume indexing)
            last_indexed_id = await get_bot_state(channel_id)
            start_id = last_indexed_id if last_indexed_id else 1

            await message.reply_text(f"Processing channel `{channel_id}` from message ID `{start_id}`...", parse_mode=enums.ParseMode.MARKDOWN)

            indexed_count = 0
            current_id = 0

            try:
                # 2. Iterate through messages from the starting ID up to the latest
                async for msg in app.get_chat_history(channel_id, offset_id=start_id, reverse=True):

                    # Check for media types that we care about
                    if not (msg.document or msg.video or msg.audio):
                        continue

                    # 3. Save the file and increment count
                    saved, count = await save_series_file_for_bulk_index(msg)
                    if saved:
                        indexed_count += count
                        total_indexed += count

                    current_id = msg.id

                    # 4. Save state every 500 messages to ensure resumption capability
                    if current_id % 500 == 0:
                        await save_bot_state(channel_id, current_id)
                        await asyncio.sleep(1) # Be polite to Telegram API
                        # Using logger.debug for less console clutter during heavy indexing
                        logger.debug(f"INDEX: Checkpoint saved for {channel_id} at ID {current_id}")

                # 5. Final state save after processing the entire channel
                if current_id > 0:
                    await save_bot_state(channel_id, current_id)

                await message.reply_text(f"✅ Finished indexing `{channel_id}`. Indexed {indexed_count} new files.", parse_mode=enums.ParseMode.MARKDOWN)

            except Exception as e:
                logger.error(f"Error indexing channel {channel_id}: {e}")
                await message.reply_text(f"❌ Indexing failed for channel `{channel_id}`: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)

        await message.reply_text(f"🥳 **BULK INDEXING COMPLETE.** Total new files indexed: **{total_indexed}**.", parse_mode=enums.ParseMode.MARKDOWN)

    return [index_command]
