# main.py - FINAL WORKING IMPLEMENTATION with index resumption

import asyncio
from pyrogram import Client, enums
from CyberTG.command import register_command_handlers
from CyberTG.index import index_files_task, lock, INDEX_STATUS_KEY, register_index_handlers
from CyberTG.filters import register_filters
from DB.database import initialize_sql_db, get_global_setting, set_global_setting
from config import API_ID, API_HASH, BOT_TOKEN, logger, ADMIN_USERS
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- New Index Resumption Checker ---
async def check_and_resume_index(cyber: Client):
    """Checks for an interrupted indexing task and resumes it."""
    global index_task

    # Only try to resume if the lock isn't currently held (shouldn't be)
    if lock.locked():
        logger.warning("Index lock is held on startup, skipping resume check.")
        return

    status = await get_global_setting(INDEX_STATUS_KEY, "IDLE")
    if not isinstance(status, str) or not status.startswith("RUNNING"):
        logger.info("No interrupted index task found to resume.")
        return

    try:
        # Format: RUNNING|<chat_id>|<current_id>|<last_msg_id>
        _, chat_id_str, start_msg_id_str, last_msg_id_str = status.split("|")
        chat_id, start_msg_id, last_msg_id = map(int, [chat_id_str, start_msg_id_str, last_msg_id_str])

        logger.warning(f"Resuming interrupted index for {chat_id} from checkpoint {start_msg_id} to {last_msg_id}...")

        admin_id = ADMIN_USERS[0] if ADMIN_USERS else None
        if not admin_id:
            logger.error("No ADMIN_USERS configured; cannot send resume notification.")
            # Still attempt to resume but with no status message:
            index_task = asyncio.create_task(index_files_task(cyber, None, chat_id, start_msg_id, last_msg_id))
            return

        # Import types here to avoid circular imports at module import time
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel Indexing", callback_data="cancel_index")]])

        msg = await cyber.send_message(
            admin_id,
            f"**[RESUMED INDEX]**\n"
            f"Indexing resumed for channel `{chat_id}`.\n"
            f"Resuming from message ID **{start_msg_id}** → **{last_msg_id}**.\n"
            f"Click cancel to stop.",
            reply_markup=kb,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        # Start the background task (start_msg_id is now the checkpoint ID)
        index_task = asyncio.create_task(
            index_files_task(cyber, msg, chat_id, start_msg_id, last_msg_id)
        )
        logger.info("Index resumption task started.")

    except Exception as e:
        logger.error(f"Failed to check or resume index task, clearing status: {e}")
        # Clear status so bot doesn't try to resume a broken task again
        try:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        except Exception:
            logger.exception("Failed to clear INDEX_STATUS_KEY")


# --- Main Asynchronous Bot Runner ---
async def run_bot_async(cyber: Client):
    """Starts the client and keeps the client alive."""
    logger.info("Starting bot client connection...")
    await cyber.start()
    logger.info("Bot is running. Waiting for updates...")

    # Register index handlers (so callback_query handlers like cancel work)
    try:
        register_index_handlers(cyber)
    except Exception as e:
        logger.exception("Failed to register index handlers: %s", e)

    # ENABLE INDEX RESUME ON STARTUP
    try:
        asyncio.create_task(check_and_resume_index(cyber))
    except Exception as e:
        logger.exception("Failed to schedule check_and_resume_index: %s", e)
        
    # --- ADDED: Send Restart Message to Admin ---
    admin_id = ADMIN_USERS[0] if ADMIN_USERS else None
    if admin_id:
        try:
            await cyber.send_message(
                admin_id,
                "**🚀 Bot Restarted Successfully!**",
                parse_mode=enums.ParseMode.MARKDOWN
            )
            logger.info(f"Sent restart notification to admin {admin_id}.")
        except Exception as e:
            logger.error(f"Failed to send restart notification to admin: {e}")
    # ---------------------------------------------

    # Keep running
    try:
        await asyncio.Future()
    except KeyboardInterrupt:
        pass

    await cyber.stop()


def main():
    """Initializes and runs the Pyrogram client, including SQL setup."""
    logger.info("Initializing and migrating data to SQL database...")
    try:
        initialize_sql_db()
        logger.info("✅ SQL database is ready.")
    except Exception as e:
        logger.error(f"❌ Critical failure during SQL DB initialization/migration: {e}")
        return

    cyber = Client(
        "SeriesBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    # Register handlers
    register_command_handlers(cyber)
    register_conversation_handlers(cyber)
    register_groupfilter_handlers(cyber)

    
    logger.info("Starting bot client main loop...")
    cyber.run(run_bot_async(cyber))


if __name__ == "__main__":
    main()
