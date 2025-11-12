# main.py - FINAL WORKING IMPLEMENTATION with index resumption

import asyncio
from pyrogram import Client, enums # Import enums for ParseMode
from CyberTG.command import register_command_handlers, index_files_task, lock, INDEX_STATUS_KEY
from CyberTG.index import index_files_task, lock, INDEX_STATUS_KEY
from conversation import register_conversation_handlers
from DB.database import initialize_sql_db, search_series, get_global_setting, set_global_setting 
from config import API_ID, API_HASH, BOT_TOKEN, logger, ADMIN_USERS 

# --- New Index Resumption Checker ---
async def check_and_resume_index(cyber: Client):
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
        msg = await cyber.send_message(
            admin_id,
            f"**[RESUMED INDEX]**\nIndexing channel `{chat_id}` resumed from ID **{start_msg_id}**.\nStatus will update every 200 messages checked.",
            parse_mode=enums.ParseMode.MARKDOWN # FIX APPLIED HERE
        )

        # Start the background task (start_msg_id is now the checkpoint ID)
        index_task = asyncio.create_task(
            # Pass the checkpoint as the starting ID
            index_files_task(cyber, msg, chat_id, start_msg_id, last_msg_id) # <<-- FIX APPLIED: Changed 'app' to 'cyber'
        )
        logger.info("Index resumption task started.")

    except Exception as e:
        logger.error(f"Failed to check or resume index task, clearing status: {e}")
        # Clear status so bot doesn't try to resume a broken task again
        await set_global_setting(INDEX_STATUS_KEY, "IDLE") 

# --- Main Asynchronous Bot Runner ---
async def run_bot_async(cyber: Client):
    """Starts the client and keeps the client alive."""
    
    logger.info("Starting bot client connection...")
    
    # 1. Start the client to establish the connection.
    await cyber.start()
    
    
    logger.info("Bot is running. Waiting for updates...")
    
    # 2. Check and Resume Index Task AFTER client start and cache load
    asyncio.create_task(check_and_resume_index(cyber)) 
    
    # 3. Keep the script alive and listening for updates.
    try:
        await asyncio.Future() 
    except KeyboardInterrupt:
        pass
    
    # 4. Cleanly stop the client on exit.
    await cyber.stop()


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
    # The 'enums' module needs to be imported in main.py if you use enums.ParseMode
    cyber = Client(
        "SeriesBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    # Register handlers
    register_command_handlers(cyber)
    register_conversation_handlers(cyber)
    
    # --- Start the bot ---
    logger.info("Starting bot client main loop...")
    cyber.run(run_bot_async(cyber))

if __name__ == "__main__":
    main()
