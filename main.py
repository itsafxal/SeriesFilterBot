# main.py - MODIFIED for handler registration

import asyncio
from pyrogram import Client
from command import register_command_handlers # New Import
from conversation import register_conversation_handlers
from database import initialize_sql_db
from config import API_ID, API_HASH, BOT_TOKEN, logger

def main():
    
    # --- SQL DB Initialization and Migration ---
    logger.info("Initializing and migrating data to SQL database...")
    try:
        def initialize_sql_db(): # Function definition starts here
    """Synchronously initializes the SQL database and runs the migration + repair.""" # Error on this line
    migrate_data_from_mongo_to_sql()
    # Optional: also repair any pre-existing rows that slipped in earlier
    try:
        repair_normalized_names_in_sqlite()
    except Exception as e:
        logger.error(f"repair_normalized_names_in_sqlite failed: {e}")

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
