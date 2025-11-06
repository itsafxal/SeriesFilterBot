# Main.py (Revised)

import asyncio
from pyrogram import Client, enums, idle
from loguru import logger
from config import API_ID, API_HASH, BOT_TOKEN
# Updated import to use the combined database logic
from database import init_db, migrate_mongo_to_sql 
from handlers.commands import register_command_handlers
from handlers.callbacks import register_callback_handlers

def build_app() -> Client:
    app = Client(
        name="series-bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    register_command_handlers(app)
    register_callback_handlers(app)
    return app

async def main():
    # 1. Initialize DB (Ensures MongoDB indexes and SQLite schema)
    init_db() 
    
    # 2. Run Migration (Synchronous, but we run it via asyncio.to_thread in database.py if using async)
    # The migrate_mongo_to_sql is now a blocking function, so we'll wrap the call 
    # to run it safely, although the main logic inside is synchronous.
    try:
        # Since migrate_mongo_to_sql is a synchronous, blocking function, 
        # we run it directly outside the async loop's execution context if possible, 
        # or inside a to_thread if you must ensure it doesn't block the startup of the main thread.
        moved = await asyncio.to_thread(migrate_mongo_to_sql)
        logger.info("Migrated {} legacy records from Mongo (if any).", moved)
    except Exception as e:
        logger.exception("Migration failed: {}", e)

    # 3. Start Bot
    app = build_app()
    await app.start()
    logger.info("Bot started.")
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
    
