# main.py
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, ForceReply

from db_manager import DBManager
from config import API_ID, API_HASH, TELEGRAM_BOT_TOKEN

# Initialize utilities
db_manager = DBManager()

# Pyrogram Client Initialization
app = Client(
    "series_progress_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TELEGRAM_BOT_TOKEN
)

# --- COMMAND HANDLERS ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    """Starts the series tracking conversation."""
    await message.reply_text(
        f"Hi **{message.from_user.first_name}**! Let's track a new series.\n"
        "Send me the **name** of the series you want to track or save.",
        reply_markup=ForceReply(True),
        parse_mode="Markdown"
    )
    # Start the series tracking flow
    await series_name_step(client, message)

@app.on_message(filters.command("list") & filters.private)
async def list_handler(client: Client, message: Message):
    """Lists all series saved by the user."""
    user_id = message.from_user.id
    user_series = db_manager.get_all_user_series(user_id)

    if not user_series:
        await message.reply_text(
            "You haven't saved any series yet. Use /start to begin tracking."
        )
        return

    reply_text = "📚 **Your Tracked Series and Progress:**\n\n"
    for item in user_series:
        title = item.get('series_title')
        season = item.get('season')
        episode = item.get('episode')
        reply_text += f"• **{title}** - S{season}E{episode}\n"

    await message.reply_text(reply_text, parse_mode="Markdown")


# --- CONVERSATION FLOW USING client.listen ---

async def series_name_step(client: Client, message: Message):
    """Listens for the series name."""
    
    # Use client.listen to wait for the next message from the same user/chat
    response: Message = await client.listen(chat_id=message.chat.id, user_id=message.from_user.id)
    series_title = response.text
    
    # Store data in user's dictionary (or a persistent state manager if needed)
    response.from_user.series_title = series_title 
    
    await response.reply_text(
        f"Tracking series: **{series_title}**.\n"
        f"Now, please send the **season number** (e.g., 1, 2, 3) you are currently watching.",
        parse_mode="Markdown"
    )
    await season_number_step(client, response)

async def season_number_step(client: Client, message: Message):
    """Listens for the season number."""
    
    response: Message = await client.listen(chat_id=message.chat.id, user_id=message.from_user.id)
    
    try:
        season = int(response.text.strip())
        if season <= 0:
            raise ValueError

        # Store season number
        response.from_user.season = season 
        
        await response.reply_text(
            f"You chose Season {season}. Now, send the **episode number** (e.g., 5, 10)."
        )
        await episode_number_step(client, response)
        
    except ValueError:
        await response.reply_text(
            "Invalid season number. Please send a valid whole number for the season. Try again."
        )
        await season_number_step(client, message) # Restart this step

async def episode_number_step(client: Client, message: Message):
    """Listens for the episode number, saves to DB, and ends the flow."""
    
    response: Message = await client.listen(chat_id=message.chat.id, user_id=message.from_user.id)
    
    try:
        episode = int(response.text.strip())
        if episode <= 0:
            raise ValueError
        
        # Retrieve stored data
        user_id = response.from_user.id
        chat_id = response.chat.id
        series_title = response.from_user.series_title
        season = response.from_user.season
        
        # 1. Save to DB
        db_manager.save_user_series(user_id, chat_id, series_title, season, episode)

        # 2. Format reply
        reply_text = (
            f"✅ **Progress Saved!**\n"
            f"You are currently on: **{series_title}** - S{season}E{episode}.\n\n"
            "Use /start to track a new series or /list to see all your progress."
        )
        await response.reply_text(reply_text, parse_mode="Markdown")

        # Cleanup stored data (optional, but good practice)
        del response.from_user.series_title
        del response.from_user.season
        
    except ValueError:
        await response.reply_text(
            "Invalid episode number. Please send a valid whole number for the episode. Try again."
        )
        await episode_number_step(client, message) # Restart this step
    except AttributeError:
         await response.reply_text(
            "Something went wrong with the session. Please use /start to begin a new tracking session."
        )
        
if __name__ == "__main__":
    app.run()
