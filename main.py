# main.py - FINAL WORKING IMPLEMENTATION without app.idle()

import asyncio
from pyrogram import Client
# Removed 'from pyrogram.errors import FloodWait' as it's no longer needed in this file
from command import register_command_handlers
from conversation import register_conversation_handlers, SERIES_CACHE, SERIES_CACHE_LC
# Removed 'get_all_users' as it's no longer used
from database import initialize_sql_db, search_series # Kept initialize_sql_db and search_series
from config import API_ID, API_HASH, BOT_TOKEN, logger

# --- Notification Function ---
# The notify_users_of_restart function is entirely removed.


# --- Main Asynchronous Bot Runner ---
async def run_bot_async(app: Client):
    """Starts the client and keeps the client alive."""
    
    logger.info("Starting bot client connection...")
    
    # 1. Start the client to establish the connection.
    await app.start()
    
    # Load Series Cache
    SERIES_CACHE[:] = await search_series("")
    SERIES_CACHE_LC[:] = [x.lower() for x in SERIES_CACHE]
    logger.info(f"SERIES CACHE LOADED: {len(SERIES_CACHE)} entries")
    
    # 2. Removed the line: asyncio.create_task(notify_users_of_restart(app))
    
    logger.info("Bot is running. Waiting for updates...")
    
    # 3. Keep the script alive and listening for updates.
    # REPLACED: await app.idle() with an infinite await
    try:
        # Await a Future that will never be done, keeping the loop running indefinitely
        await asyncio.Future() 
    except KeyboardInterrupt:
        # Allows cleanup on Ctrl+C or process termination
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
    # This is the blocking call that executes the async function run_bot_async
    app.run(run_bot_async(app))

if __name__ == "__main__":
    main()
