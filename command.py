# command.py

import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import logger, ADMIN_USERS, INDEX_CHANNELS
from database import (
    get_bot_state,
    save_bot_state,
    get_global_setting, 
    set_global_setting,
    initialize_sql_db # Required for /index which triggers migration
)
from utils import save_series_file_for_bulk_index # Required for /index

def register_command_handlers(app: Client):
    """Registers all command-based message handlers with the Pyrogram client."""

    @app.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):
        """Handles the /start command."""
        welcome_text = (
            "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
            "Just send me the <b>name of a series</b> you are looking for. "
            "I'll show you a summary and ask you to select the **Season**."
        )
        await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

    @app.on_message(filters.command("index") & filters.user(ADMIN_USERS) & filters.private)
    async def index_command(_, message: Message):
        """Admin command to start bulk indexing of files from configured channels."""
        
        # NOTE: initialize_sql_db is run sync in main.py, but including it in case 
        # a full re-migration is needed on command (though usually it runs once at startup).
        # For a safer/cleaner implementation, consider re-running initialize_sql_db() here 
        # as a fresh migration/re-index if the user is an admin.
        
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
                        min_id=start_id,
                        reverse=True):     # this fixes indexing stop bug

                    current_id = msg.id

                    if not (msg.document or msg.video or msg.audio):
                        continue

                    # save_series_file_for_bulk_index is from utils.py
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

    @app.on_message(filters.command("imdb") & filters.user(ADMIN_USERS) & filters.private)
    async def poster_toggle_command(_, message: Message):
        """Allows admin to turn the IMDB poster feature on or off."""
        current_state = await get_global_setting("IMDB_POSTER_ENABLED", True) 
        new_state = not current_state
        await set_global_setting("IMDB_POSTER_ENABLED", new_state)

        status = "✅ **ON**" if new_state else "❌ **OFF**"
        await message.reply_text(
            f"🖼️ IMDB Poster feature is now set to: {status}",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    @app.on_message(filters.command("autodel") & filters.user(ADMIN_USERS) & filters.private)
    async def autodel_command(_, message: Message):
        """Allows admin to set the auto-delete timer for sent files."""
        try:
            if len(message.command) > 1:
                timer = int(message.command[1])
                if timer < 0:
                    timer = 0
                
                await set_global_setting("AUTO_DELETE_TIMER", timer)
                
                if timer > 0:
                    status = f"✅ **ON**: Menu auto-delete timer set to **{timer} seconds**."
                else:
                    status = "❌ **OFF**: Menu auto-delete is disabled."
                    
                await message.reply_text(
                    f"🗑️ Menu Auto-Deletion feature is now set to: {status}\n\n"
                    f"Set to 0 to disable.",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            else:
                current_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
                status = f"Current menu auto-delete timer is: **{current_timer} seconds**."
                await message.reply_text(
                    f"Usage: `/autodel <seconds>` (e.g., `/autodel 300`)\n"
                    f"{status}",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                
        except ValueError:
            await message.reply_text("❌ Invalid number provided. Please use an integer for seconds.", parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Error in autodel_command: {e}")

    # The function must return a list of handlers for registration in main.py
    return [
        start_command, 
        index_command, 
        poster_toggle_command, 
        autodel_command
    ]
