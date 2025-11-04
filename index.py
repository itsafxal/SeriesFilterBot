# index.py
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
# Assumes config.py exists
from config import INDEX_CHANNELS, ADMIN_USERS, logger
# Assumes database.py and utils.py exist
from database import save_bot_state, get_bot_state
from utils import save_series_file_for_bulk_index

def register_media_handler(app: Client):

    # FIX: Corrected filter precedence using parentheses.
    @app.on_message((filters.document | filters.video | filters.audio) & filters.channel)
    async def media_handler(_, message: Message):
        """
        Handles single file posts in indexed channels (Real-Time Indexing).
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
            # We start processing from the message *after* the last indexed ID.
            start_id = last_indexed_id if last_indexed_id else 0 

            await message.reply_text(f"Processing channel `{channel_id}` from message ID `{start_id + 1}`...", parse_mode=enums.ParseMode.MARKDOWN)

            indexed_count = 0
            current_id = 0

            try:
                # 2. Iterate through all messages (newest to oldest is default).
                async for msg in app.get_chat_history(channel_id):

                    current_id = msg.id

                    # FIX: Stop and break if we reach a message ID that was already processed.
                    if current_id <= start_id:
                        logger.info(f"INDEX: Reached last indexed message ID {start_id} for {channel_id}. Stopping iteration.")
                        break

                    # --- FIXED MEDIA CHECK (More robust for Pyrogram's get_chat_history) ---
                    # Check for media objects (document, video, or audio)
                    if not (msg.document or msg.video or msg.audio):
                         continue
                    # --- END FIXED MEDIA CHECK ---

                    # 3. Save the file and increment count
                    # The utils function is now more robust at extracting file info from forwarded messages.
                    saved, count = await save_series_file_for_bulk_index(msg)
                    if saved:
                        indexed_count += count
                        total_indexed += count

                    # 4. Save state every 500 messages to ensure resumption capability
                    if indexed_count > 0 and indexed_count % 500 == 0:
                        # Save the ID of the message we just processed (current_id)
                        await save_bot_state(channel_id, current_id)
                        await asyncio.sleep(1) # Be polite to Telegram API
                        logger.debug(f"INDEX: Checkpoint saved for {channel_id} at ID {current_id}")

                # 5. Final state save after processing the remaining messages up to the break point.
                # Only save if we actually processed *new* messages (i.e., current_id is greater than start_id)
                if current_id > start_id:
                    await save_bot_state(channel_id, current_id)

                await message.reply_text(f"✅ Finished indexing `{channel_id}`. Indexed {indexed_count} new files.", parse_mode=enums.ParseMode.MARKDOWN)

            except Exception as e:
                logger.error(f"Error indexing channel {channel_id}: {e}")
                await message.reply_text(f"❌ Indexing failed for channel `{channel_id}`: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)

        await message.reply_text(f"🥳 **BULK INDEXING COMPLETE.** Total new files indexed: **{total_indexed}**.", parse_mode=enums.ParseMode.MARKDOWN)

    return [index_command]
