# main.py - MODIFIED for SQL DB Initialization

import asyncio
from pyrogram import Client
# Assumes 'index' module exists with necessary imports
from index import register_media_handler, register_bulk_index_handlers
from conversation import register_conversation_handlers
from database import initialize_sql_db # Import the new initialization function
from state_manager import get_bot_state, save_bot_state
from config import API_ID, API_HASH, BOT_TOKEN, logger

def main():
    """Initializes and runs the Pyrogram client, including SQL setup."""
    
    # --- SQL DB Initialization and Migration ---
    # This must run before the bot starts to ensure the SQL DB is ready.
    logger.info("Initializing and migrating data to SQL database...")
    try:
        # Since initialize_sql_db performs blocking I/O, we run it sync or 
        # use a dedicated loop/thread if needed. For a simple setup, running it sync here is typical.
        initialize_sql_db()
        logger.info("✅ SQL database is ready.")
    except Exception as e:
        logger.error(f"❌ Critical failure during SQL DB initialization/migration: {e}")
        # Depending on criticality, you might want to sys.exit(1) here
        return
        
    # --- Pyrogram Client Setup ---
    app = Client(
        "SeriesBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    # Register handlers
    # 1. Conversation handlers (Search, Menus, Quality Filter)
    register_conversation_handlers(app)

    # 2. Indexing handlers (RTI, Bulk Index)
    register_media_handler(app)
    register_bulk_index_handlers(app) 
    
    # Start the bot
    logger.info("Starting bot client...")
    app.run()

if __name__ == "__main__":
    main()
