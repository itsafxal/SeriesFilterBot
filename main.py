from gotgbot.api import Bot
from gotgbot.ext import Application, CommandHandler
import asyncio
import sys

# Import the Database instance (which uses pymongo) and the handlers
from conversation import series_handler, other_handlers
from db import db 

async def main() -> None:
    """Starts the bot."""
    TOKEN = "YOUR_BOT_TOKEN" 
    
    if TOKEN == "YOUR_BOT_TOKEN":
        print("ERROR: Please replace 'YOUR_BOT_TOKEN' with your actual bot token.")
        sys.exit(1)

    # 1. Initialize Bot and Application
    bot = Bot(token=TOKEN)
    application = Application(bot=bot)

    # 2. Add Handlers
    application.add_handler(series_handler)
    for handler in other_handlers:
        application.add_handler(handler)
        
    print("Bot started. Polling for updates...")
    
    # 3. Start the bot 
    await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")
    except Exception as e:
        print(f"An error occurred in main: {e}")
    finally:
        # Crucial step: Close the pymongo connection when the bot stops
        db.close()
