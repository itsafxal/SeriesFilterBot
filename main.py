import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from bot.handlers import start_command, help_command, filter_series, error_handler
from bot.database import initialize_db

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
# NOTE: Replace 'YOUR_BOT_TOKEN' with your actual Telegram Bot Token
BOT_TOKEN = '7661197159:AAHwppy1JjmEBXucY1gxdbfGKVpuqjl_gFg' 
# In main.py
# ... (Other definitions) ...
ADMIN_USER_ID = "5483128891" # <--- REPLACE with your Telegram User ID!
# ... (Other definitions) ...

def main():
    """Start the bot."""
    # Initialize the database connection
    db = initialize_db()
    
    # Create the Application and pass your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Register Handlers ---
    # Command Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addfile", lambda update, context: addfile_command(update, context, db_collection, ADMIN_USER_ID)))

    # Message Handler (for filtering - this is where the core logic will go)
    # The handler will use a context-aware callback to pass the DB connection.
    
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, lambda update, context: filter_series(update, context, db)
    ))

    # Error Handler
    application.add_error_handler(error_handler)

    # Start the Bot
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
