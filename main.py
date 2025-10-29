import re
import os
import asyncio
import logging
from gotgbot.ext import Application, Defaults
from gotgbot.ext.handlers import CommandHandler, MessageHandler, CallbackQueryHandler
from gotgbot.ext.filters import TEXT, COMMANDS, MessageFilter
from gotgbot import Bot
from gotgbot.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    Update, 
    Message
)
from motor.motor_asyncio import AsyncIOMotorClient # Reverted to async motor

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Disable logging for httpx, which is used by gotgbot internally
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# --- CONFIGURATION ---
API_ID = 21352077 
API_HASH = "e4ba74d3f410dffbe848402989e61a04"
BOT_TOKEN = "7661197159:AAHwppy1JjmEBXucY1gxdbfGKVpuqjl_gFg"
MONGO_URI = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0" 
DUMP_CHANNEL_ID = int(os.environ.get("DUMP_CHANNEL_ID", -1001996538290))
OWNER_ID = int(os.environ.get("OWNER_ID", 5483128891)) 

# --- Database Setup (Async Motor) ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
files_collection = db["files"]

# NOTE: Indices are created automatically on the first connection or insert,
# or you can run them manually if needed.
# files_collection.create_index([("normalized_name", 1), ("season", 1)])
# files_collection.create_index([("normalized_name", "text")])


# --- User Client (Still Pyrogram for Channel History) ---
# NOTE: Gotgbot does not index channels. We keep the Pyrogram User Client 
# to fetch channel history for the /index command.
from pyrogram import Client as PyrogramClient

user_app = PyrogramClient(
    "IndexerUser",
    api_id=API_ID,
    api_hash=API_HASH
)

# ###################################################
# ## DATABASE FUNCTIONS (Async Motor) ##
# ###################################################

async def search_series(query: str):
    """Searches MongoDB for unique series names matching the query."""
    regex_query = re.escape(query).replace(" ", ".*")
    series_names = await files_collection.distinct(
        "normalized_name", 
        {"normalized_name": {"$regex": regex_query, "$options": "i"}}
    )
    return series_names

async def get_unique_seasons_by_name(series_name: str):
    """Gets unique season numbers for a given series."""
    seasons = await files_collection.distinct("season", {"normalized_name": series_name})
    seasons = sorted([s for s in seasons if s > 0])
    return seasons

async def get_all_files_by_season(series_name: str, season: int):
    """Gets file details (channel_id, message_id) for a specific season."""
    files_cursor = files_collection.find(
        {"normalized_name": series_name, "season": season},
        {"channel_id": 1, "message_id": 1, "_id": 0}
    )
    return await files_cursor.to_list(length=None)
    
async def index_files_in_db(file_docs):
    """Inserts multiple file documents into MongoDB."""
    if file_docs:
        result = await files_collection.insert_many(file_docs)
        return len(result.inserted_ids)
    return 0

async def clear_index():
    """Clears all documents from the collection."""
    await files_collection.delete_many({})

# ###################################################
# ## UTILITY & PARSING ##
# ###################################################

def parse_series_info(file_name: str):
    """
    Attempts to extract Series Name, Season (SXX), and Episode (EXX) from a filename.
    Returns: (normalized_name, season_number, episode_number)
    """
    if not file_name:
        return "", 0, 0
        
    match = re.search(r"[sS](\d+)[eE](\d+)", file_name)
    
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        
        normalized_name = re.sub(r"([. _-]+)?[sS]\d+[eE]\d+([. _-]+)?", " ", file_name).strip()
        
        for term in ["4K", "1080P", "720P", "HDRIP", "WEB-DL", "AMZN", "NETFLIX", "DDP5.1", "X265", "x264", "MULTISUB", "AAC"]:
            normalized_name = re.sub(r"\b" + re.escape(term) + r"\b", "", normalized_name, flags=re.IGNORECASE).strip()

        normalized_name = re.sub(r"[. _]+", " ", normalized_name).title().strip()
        
        return normalized_name, season, episode
    
    return file_name.title().strip(), 0, 0 

# ###################################################
# ## GOTGBOT HANDLERS ##
# ###################################################

async def start_command(bot: Bot, update: Update):
    """Handles the /start command and welcomes the user."""
    welcome_text = (
        "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
        "Just send me the <b>name of a series</b> you are looking for, "
        "and I'll show you the available seasons and episodes from the index.\n\n"
        "<b>Example:</b> <code>The Last of Us</code>\n\n"
        f"<b>Admin:</b> Use <code>/index</code> to update the file database."
    )
    if update.effective_message:
        await update.effective_message.reply_html(welcome_text)

async def search_handler(bot: Bot, update: Update):
    """Handles user text search queries and initiates the series selection menu."""
    if not update.effective_message or not update.effective_message.text:
        return

    query = update.effective_message.text.strip()
    
    # Calls the Motor wrapper
    unique_names = await search_series(query)
    
    if unique_names:
        keyboard = []
        for name in sorted(unique_names):
            callback_data = f'series_select|{name}'
            button_text = name[:50] + '...' if len(name) > 50 else name
            keyboard.append([InlineKeyboardButton(button_text, callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"🔎 Found {len(unique_names)} matching series. Please select one:"
        await update.effective_message.reply_text(response, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(f"❌ No series files found matching '{query}'.")

async def callback_query_handler(bot: Bot, update: Update):
    """Handles button clicks for series and season selection."""
    if not update.callback_query or not update.callback_query.data:
        await update.callback_query.answer("Invalid callback data.", show_alert=True)
        return

    query = update.callback_query
    data = query.data.split('|')
    action_type = data[0]
    
    try:
        if action_type == 'series_select':
            series_name = data[1]
            await show_season_menu(query.message, series_name)
            
        elif action_type == 'season_send':
            series_name = data[1]
            season = int(data[2])
            await send_season_files(query.message, series_name, season)
    except Exception as e:
        logger.error(f"Error in callback: {e}")
        await query.answer("An internal error occurred. Try again.", show_alert=True)
    
    await query.answer()

async def show_season_menu(message: Message, series_name: str):
    """Displays buttons for all unique seasons of the selected series."""
    # Calls the Motor wrapper
    seasons = await get_unique_seasons_by_name(series_name)
    
    if not seasons:
        await message.edit_text(text=f"❌ No seasons found for {series_name}.")
        return

    keyboard = []
    row = []
    for season in seasons:
        callback_data = f'season_send|{series_name}|{season}'
        button_text = f"S{season:02d}"
        row.append(InlineKeyboardButton(button_text, callback_data))
        
        if len(row) >= 5: 
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    caption = f"📺 {series_name}\n\nSelect a Season:"
    
    await message.edit_text(caption, reply_markup=reply_markup)


async def send_season_files(message: Message, series_name: str, season: int):
    """Sends all files belonging to the selected season using copy_message."""
    user_chat_id = message.chat.id
    # Calls the Motor wrapper
    files = await get_all_files_by_season(series_name, season)
    
    if not files:
        await message.reply_text(text=f"❌ No files found for {series_name} Season {season:02d}.")
        return

    status_message = await message.reply_text(
        f"📦 Sending {len(files)} files for {series_name} Season {season:02d}..."
    )

    success_count = 0
    # NOTE: Gotgbot's Bot.copy_message is the preferred way
    bot = message.bot 
    
    for file_entry in files:
        channel_id = file_entry.get('channel_id')
        message_id = file_entry.get('message_id')
        
        if channel_id and message_id:
            try:
                # Use gotgbot's copy_message
                await bot.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id
                )
                success_count += 1
                await asyncio.sleep(0.5) # Flood limit buffer
            except Exception as e:
                logger.error(f"Failed to copy message {message_id} from {channel_id}: {e}")

    final_message = f"✅ Sent {success_count} files for {series_name} Season {season:02d}."
    
    try:
        await status_message.edit_text(final_message)
    except Exception:
        await message.reply_text(final_message)


async def index_channel_files_command(bot: Bot, update: Update):
    """Command to start indexing files from the DUMP_CHANNEL_ID (Owner Only)."""
    if update.effective_user and update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text("⛔️ You are not authorized to run this command.")
        return
    
    status_message = await update.effective_message.reply_text(
        f"⏳ Starting index of files from channel {DUMP_CHANNEL_ID}. This may take a while..."
    )
    
    # 1. Start the Pyrogram User Client
    try:
        await user_app.start()
        logger.info("Pyrogram User Client started for indexing.")
    except Exception as e:
        error_msg = f"❌ Failed to start User Client for indexing. Check session file/login: {e}"
        await status_message.edit_text(error_msg)
        logger.error(error_msg)
        return

    # 2. Clear the old index
    await clear_index() 
    await status_message.edit_text(f"Indexing... Cleared old index. Starting file processing.")

    # 3. Call the indexing function
    total_indexed = await index_channel_files(status_message)
    
    # 4. Stop the Pyrogram User Client
    await user_app.stop() 
    
    final_message = f"🎉 Indexing Complete!\n\nTotal files indexed: {total_indexed}."
    await status_message.edit_text(final_message)


async def index_channel_files(status_message: Message):
    """Iterates through a channel and stores file metadata in MongoDB."""
    
    indexed_count = 0
    buffer = [] # Buffer for bulk inserts
    
    try:
        # Use the Pyrogram User Client (user_app) to read history
        async for msg in user_app.get_chat_history(DUMP_CHANNEL_ID): 
            
            if msg.document or msg.video:
                file_name = msg.document.file_name if msg.document else (msg.video.file_name or "")
                
                normalized_name, season, episode = parse_series_info(file_name)
                
                if season > 0: 
                    file_doc = {
                        "channel_id": DUMP_CHANNEL_ID,
                        "message_id": msg.id,
                        "file_name": file_name,
                        "normalized_name": normalized_name,
                        "season": season,
                        "episode": episode,
                        "timestamp": msg.date
                    }
                    
                    buffer.append(file_doc)
                    
                    if len(buffer) >= 50:
                        # Bulk insert into DB (Motor)
                        await index_files_in_db(buffer)
                        indexed_count += len(buffer)
                        buffer = []
                        
                        # Update status message
                        await status_message.edit_text(f"Indexing... {indexed_count} files processed so far.")
                
                await asyncio.sleep(0.05)
                
        # Insert any remaining documents in the buffer
        if buffer:
            await index_files_in_db(buffer)
            indexed_count += len(buffer)
            
    except Exception as e:
        await status_message.edit_text(f"❌ An unknown error occurred during indexing: {e}")
        logger.error(f"Indexing Error: {e}")
            
    return indexed_count


# --- Startup Hook for Gotgbot ---

async def post_init(application: Application) -> None:
    """Sends restart message after the bot is fully started."""
    bot = application.bot
    # Use the bot's own ID to send a dummy message to check status,
    # then send the restart message.
    try:
        # Use bot.send_message to send the notification
        await bot.send_message(
            chat_id=OWNER_ID,
            text="✨ <b>Bot Restarted!</b>\n\nFile filter service is online and ready.",
            parse_mode="HTML"
        )
        logger.info("Sent restart message to owner.")
    except Exception as e:
        logger.error(f"Failed to send restart message to owner: {e}")


# ###################################################
# ## MAIN EXECUTION ##
# ###################################################

def main() -> None:
    """Start the bot."""
    
    # 1. Setup Gotgbot Application
    defaults = Defaults(parse_mode="HTML")
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .post_init(post_init) # Add the startup hook
        .build()
    )

    # 2. Setup Handlers
    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("index", index_channel_files_command))
    
    # Message handler (non-commands, non-private)
    # Filter for private chat and non-command text
    private_text_filter = TEXT & ~COMMANDS & MessageFilter(lambda msg: msg.chat.type == 'private')
    application.add_handler(MessageHandler(private_text_filter, search_handler))
    
    # Callback query handler
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    # 3. Run the Bot
    logger.info("Starting Gotgbot...")
    application.run_polling(poll_interval=1.0)

if __name__ == "__main__":
    main()
