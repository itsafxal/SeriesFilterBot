from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN, logger
from index import register_media_handler, register_bulk_index_handlers
from conversation import register_conversation_handlers

# --- Pyrogram Client Setup ---
app = Client(
    "SeriesFilterBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

async def send_startup_notification(client):
    logger.info("Bot restarted successfully.")

def main():
    # Register all handlers
    register_media_handler(app)
    register_bulk_index_handlers(app)
    register_conversation_handlers(app)
    
    # Run the bot
    logger.info("Starting Pyrogram bot...")
    app.run()

if __name__ == "__main__":
    main()
