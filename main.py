# main.py - FINAL WORKING IMPLEMENTATION with index resumption

import asyncio
from pyrogram import Client
from command import register_command_handlers, index_files_task, lock, INDEX_STATUS_KEY
from conversation import register_conversation_handlers
from database import initialize_sql_db, search_series, get_global_setting, set_global_setting # ADD get_global_setting, set_global_setting
from config import API_ID, API_HASH, BOT_TOKEN, logger, ADMIN_USERS # ADD ADMIN_USERS

# --- New Index Resumption Checker ---
async def check_and_resume_index(app: Client):
    """Checks for an interrupted indexing task and resumes it."""
    global index_task
    
    # Only try to resume if the lock isn't currently held (shouldn't be)
    if lock.locked():
        logger.warning("Index lock is held on startup, skipping resume check.")
        return

    status = await get_global_setting(INDEX_STATUS_KEY, "IDLE")
    if not status.startswith("RUNNING"):
        logger.info("No interrupted index task found to resume.")
        return

    try:
        # Format: RUNNING|<chat_id>|<current_id>|<last_msg_id>
        _, chat_id_str, start_msg_id_str, last_msg_id_str = status.split("|")
        chat_id, start_msg_id, last_msg_id = map(int, [chat_id_str, start_msg_id_str, last_msg_id_str])

        logger.warning(f"Resuming interrupted index for {chat_id} from checkpoint {start_msg_id} to {last_msg_id}...")
        
        # We need an admin's chat ID to send the status updates.
        # Fallback to a message object that sends new updates to the primary admin
        admin_id = ADMIN_USERS[0] 
        
        # Send a starting notification and get the message object for updates
        msg = await app.send_message(
            admin_id,
            f"**[RESUMED INDEX]**\nIndexing channel `{chat_id}` resumed from ID **{start_msg_id}**.\nStatus will update every 200 messages checked.",
            parse_mode="markdown"
        )

        # Start the background task (start_msg_id is now the checkpoint ID)
        index_task = asyncio.create_task(
            # Pass the checkpoint as the starting ID
            index_files_task(app, msg, chat_id, start_msg_id, last_msg_id) 
        )
        logger.info("Index resumption task started.")

    except Exception as e:
        logger.error(f"Failed to check or resume index task, clearing status: {e}")
        # Clear status so bot doesn't try to resume a broken task again
        await set_global_setting(INDEX_STATUS_KEY, "IDLE") 

# --- Main Asynchronous Bot Runner ---
async def run_bot_async(app: Client):
    """Starts the client and keeps the client alive."""
    
    logger.info("Starting bot client connection...")
    
    # 1. Start the client to establish the connection.
    await app.start()
    
    
    logger.info("Bot is running. Waiting for updates...")
    
    # 2. Check and Resume Index Task AFTER client start and cache load
    asyncio.create_task(check_and_resume_index(app)) 
    
    # 3. Keep the script alive and listening for updates.
    try:
        await asyncio.Future() 
    except KeyboardInterrupt:
        pass
    
    # 4. Cleanly stop the client on exit.
    await app.stop()


def main():
    """Initializes and runs the Pyrogram client, including SQL setup."""
    
    # --- SQL DB Initialization and Migration ---
    logger.info("Initializing and migrating data to SQL database...")
    try:
        initialize_sql_db()
        logger.info("✅ SQL database is ready.")
    except Exception as e:
        logger.error(f"❌ Critical failure during SQL DB initialization/migration: {e}")
        return
        
    # --- Pyrogram Client Setup ---
    app = Client(
        "SeriesBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    # Register handlers
    register_command_handlers(app)
    register_conversation_handlers(app)
    
    # --- Start the bot ---
    logger.info("Starting bot client main loop...")
    app.run(run_bot_async(app))

if __name__ == "__main__":
    main()
