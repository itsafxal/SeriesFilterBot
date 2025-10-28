import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from bot.handlers import start_command, help_command, filter_series, error_handler
from bot.database import initialize_db

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
# NOTE: Replace 'YOUR_BOT_TOKEN' with your actual Telegram Bot Token
BOT_TOKEN = 'YOUR_BOT_TOKEN' 

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
