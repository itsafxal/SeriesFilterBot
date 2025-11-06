# main.py - MODIFIED for handler registration

import asyncio
from pyrogram import Client
from command import register_command_handlers # New Import
from conversation import register_conversation_handlers
from database import initialize_sql_db
from config import API_ID, API_HASH, BOT_TOKEN, logger

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

    # Register handlers from both files
    register_command_handlers(app) # Register command handlers
    register_conversation_handlers(app) # Register conversation and other non-command handlers
    
    # Start the bot
    logger.info("Starting bot client...")
    app.run()

if __name__ == "__main__":
    main()
