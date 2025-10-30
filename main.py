# main.py

from pyrogram import Client
from index import register_media_handler, register_bulk_index_handlers
from conversation import register_conversation_handlers
from config import API_ID, API_HASH, BOT_TOKEN, logger

def main():
    """Initializes and runs the Pyrogram client."""
    
    app = Client(
        "SeriesBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    # Register handlers
    
    # 1. Conversation handlers (Search, Menus, Quality Filter)
    conversation_handlers = register_conversation_handlers(app)
    # The handlers are registered via decorators inside the function, 
    # but the return is used if needed by a specific main loop structure.

    # 2. Indexing handlers (RTI, Bulk Index)
    register_media_handler(app)
    register_bulk_index_handlers(app) 
    
    # Start the bot
    logger.info("Starting bot client...")
    app.run()

if __name__ == "__main__":
    main()
