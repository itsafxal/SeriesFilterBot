# bot_main.py (Complete Code for Real-time Series Indexing Bot - Revised)

import re
import os
import asyncio
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from pymongo import MongoClient, TEXT

# --- Configuration Placeholders (Your actual values) ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "7661197159:AAHwppy1JjmEBXucY1gxdbfGKVpuqjl_gFg"
MONGO_URI = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# List of channel IDs (integers or strings) where files are posted and should be indexed.
CHANNELS = [-1001996538290] 

# --- Setup Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- Database Setup (Synchronous PyMongo) ---
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
files_collection = db["files"]

# Ensure indices are created (PyMongo needs this done once)
files_collection.create_index([("normalized_name", 1), ("season", 1)])
files_collection.create_index([("normalized_name", TEXT)])

# --- Pyrogram Client Setup ---
app = Client(
    "SeriesFilterBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)


###################################################
## DATABASE WRAPPER FUNCTIONS (PyMongo via asyncio.to_thread) ##
###################################################

async def search_series(query: str):
    """Searches MongoDB for unique series names matching the query."""
    def sync_search():
        # Escaping and then replacing spaces with '.*' allows flexible searching
        regex_query = re.escape(query).replace(" ", ".*")
        pipeline = [
            {"$match": {"normalized_name": {"$regex": regex_query, "$options": "i"}}},
            {"$group": {"_id": "$normalized_name"}},
            {"$project": {"_id": 0, "name": "$_id"}}
        ]
        return [doc["name"] for doc in files_collection.aggregate(pipeline)]
        
    return await asyncio.to_thread(sync_search)

async def get_unique_seasons_by_name(series_name: str):
    """Gets unique season numbers for a given series."""
    def sync_get_seasons():
        seasons = files_collection.distinct("season", {"normalized_name": series_name})
        # Filter out season 0 (which is often used for non-series files) and sort
        seasons = sorted([s for s in seasons if s > 0])
        return seasons
        
    return await asyncio.to_thread(sync_get_seasons)

async def get_all_files_by_season(series_name: str, season: int):
    """Gets file details (channel_id, message_id) for a specific season."""
    def sync_get_files():
        files = files_collection.find(
            {"normalized_name": series_name, "season": season},
            {"channel_id": 1, "message_id": 1, "_id": 0}
        )
        return list(files)
        
    return await asyncio.to_thread(sync_get_files)
    
async def save_file_to_db(file_doc: dict):
    """Inserts a single file document into MongoDB."""
    def sync_insert_one():
        try:
            # Check for existing document to prevent duplicates (optional, but good)
            files_collection.update_one(
                {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
                {"$set": file_doc},
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Failed to insert file doc: {e}")
            return False
            
    return await asyncio.to_thread(sync_insert_one)

# ###################################################
# ## UTILITY & PARSING ##
# ###################################################

def parse_series_info(file_name: str):
    """
    Attempts to extract Series Name, Season (SXX), and Episode (EXX) from a filename.
    
    REVISED: Added better cleanup for embedded tags like [Series World Tm] and technical tags.
    """
    if not file_name:
        return "", 0, 0
        
    # Regex to find SXXEXX pattern
    match = re.search(r"[sS](\d+)[eE](\d+)", file_name)
    
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        
        # 1. Remove the SXXEXX part and surrounding separators
        normalized_name = re.sub(r"([. _-]+)?[sS]\d+[eE]\d+([. _-]+)?", " ", file_name).strip()
        
        # 2. Clean common garbage terms (case-insensitive)
        # BOLD: Added common technical tags that caused duplicates (10BIT, MKV, WEBRIP, etc.)
        common_terms = ["4K", "1080P", "720P", "HDRIP", "WEB-DL", "AMZN", "NETFLIX", 
                        "DDP5.1", "X265", "x264", "MULTISUB", "AAC", "10BIT", "MKV", "WEBRIP"] 
        
        for term in common_terms:
            # Use \b for word boundaries
            normalized_name = re.sub(r"\b" + re.escape(term) + r"\b", "", normalized_name, flags=re.IGNORECASE).strip()

        # 3. Aggressively remove bracketed/parenthesized channel/source tags (e.g., [@Series World Tm])
        # BOLD: This step specifically removes the " [Series World Tm] " part causing your duplicates.
        normalized_name = re.sub(r"\[.*?\]|\(.*?\)", " ", normalized_name).strip()
        
        # 4. Replace multiple spaces/dots/underscores with a single space, then Title case it
        normalized_name = re.sub(r"[. _]+", " ", normalized_name).title().strip()
        
        return normalized_name, season, episode
    
    # If SXXEXX is not found, treat it as a potential standalone file, with Season/Episode 0
    return file_name.title().strip(), 0, 0 


# ###################################################
# ## REAL-TIME FILE HANDLER (INDEXING LOGIC) ##
# ###################################################

# Filter for document, video, or audio messages
media_filter = filters.document | filters.video | filters.audio

@app.on_message(filters.chat(CHANNELS) & media_filter)
async def media_handler(_, message):
    """
    Handles new media messages in the configured channels and indexes them if they are a series file.
    """
    
    file_name = ""
    # Prioritize file name from document/video
    if message.document:
        file_name = message.document.file_name or ""
    elif message.video:
        file_name = message.video.file_name or ""
    else: # Fallback for audio
        file_name = getattr(message.audio, "file_name", "")

    normalized_name, season, episode = parse_series_info(file_name)
    
    # Only index files that were successfully parsed as part of a series (Season > 0)
    if season > 0:
        file_doc = {
            "channel_id": message.chat.id,
            "message_id": message.id,
            "file_name": file_name,
            "normalized_name": normalized_name,
            "season": season,
            "episode": episode,
            "timestamp": message.date
        }
        
        saved = await save_file_to_db(file_doc)
        if saved:
            logger.info(f"Indexed new file: {normalized_name} S{season:02d}E{episode:02d} from message {message.id}")

# ###################################################
# ## PYROGRAM HANDLERS (User Interaction) ##
# ###################################################

# --- Startup Logic (Now simplified) ---
async def send_startup_notification(client):
    """A minimal placeholder, as OWNER_ID is removed."""
    logger.info("Bot restarted successfully.")


# Handler for the /start command
@app.on_message(filters.command("start") & filters.private)
async def start_command(_, message):
    """Handles the /start command."""
    welcome_text = (
        "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
        "I automatically index new files posted in the archive channels.\n\n"
        "Just send me the <b>name of a series</b> you are looking for, "
        "and I'll show you the available seasons and episodes from the index.\n\n"
        "<b>Example:</b> <code>Loki</code>"
    )
    await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

# Handler for plain text messages in private chat
@app.on_message(filters.text & filters.private & ~filters.command("start"))
async def search_handler(_, message):
    """Handles user text search queries."""
    query = message.text.strip()
    
    unique_names = await search_series(query) 
    
    if unique_names:
        keyboard = []
        # Sort names alphabetically
        for name in sorted(unique_names):
            # The action is always 'series_select'
            callback_data = f'series_select|{name}'
            # Truncate long names for button text
            button_text = name[:50] + '...' if len(name) > 50 else name
            
            # Use a full button row for the series name
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"🔎 Found {len(unique_names)} matching series. Please select one:"
        await message.reply_text(response, reply_markup=reply_markup)
    else:
        await message.reply_text(f"❌ No series files found matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)

# Handler for inline button presses
@app.on_callback_query(filters.regex(r"series_select|season_send"))
async def callback_query_handler(_, query):
    """Handles button clicks for series and season selection."""
    data = query.data.split('|')
    action_type = data[0]
    
    try:
        if action_type == 'series_select':
            series_name = data[1]
            await show_season_menu(query, series_name)
            
        elif action_type == 'season_send':
            series_name = data[1]
            season = int(data[2])
            await send_season_files(query, series_name, season)
    except Exception as e:
        logger.error(f"Error in callback: {e}")
        await query.answer("An internal error occurred. Try again.", show_alert=True)
    
    await query.answer()

async def show_season_menu(query, series_name: str):
    """Displays buttons for all unique seasons of the selected series."""
    seasons = await get_unique_seasons_by_name(series_name) 
    
    if not seasons:
        try:
            await query.message.edit_text(text=f"❌ No seasons found for <b>{series_name}</b>.", parse_mode=enums.ParseMode.HTML) 
        except MessageNotModified:
            pass
        return

    # Count the total episodes/files for the series
    total_files = sum(len(await get_all_files_by_season(series_name, s)) for s in seasons)
    
    keyboard = []
    row = []
    for season in seasons:
        callback_data = f'season_send|{series_name}|{season}'
        button_text = f"Season {season}"
        
        row.append(InlineKeyboardButton(button_text, callback_data=callback_data))
        
        if len(row) >= 4: # 4 buttons per row for better layout
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    # Added total files count to the caption
    caption = (
        f"📺 <b>{series_name}</b>\n"
        f"<i>Total Files Indexed: {total_files} in {len(seasons)} seasons.</i>\n\n"
        "Select a Season to get the files:"
    )
    
    try:
        await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.HTML)
    except MessageNotModified:
        pass


async def send_season_files(query, series_name: str, season: int):
    """Sends all files belonging to the selected season using copy_message."""
    user_chat_id = query.message.chat.id
    files = await get_all_files_by_season(series_name, season) 
    
    if not files:
        await query.message.reply_text(text=f"❌ No files found for <b>{series_name}</b> Season {season:02d}.", parse_mode=enums.ParseMode.HTML) 
        return

    status_message = await query.message.reply_text(
        f"📦 Sending {len(files)} files for <b>{series_name}</b> Season {season:02d}..."
    )

    success_count = 0
    
    for file_entry in files:
        channel_id = file_entry.get('channel_id')
        message_id = file_entry.get('message_id')
        
        if channel_id and message_id:
            try:
                # Use copy_message to forward the file without the 'via @botname' tag
                await app.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id
                )
                success_count += 1
                await asyncio.sleep(0.5) # Throttle to avoid flooding Telegram
            except Exception as e:
                logger.error(f"Failed to copy message {message_id} from {channel_id}: {e}")

    final_message = f"✅ Sent <b>{success_count}</b> files for <b>{series_name}</b> Season {season:02d}."
    
    # Try to edit the status message, fall back to a new message if it fails
    try:
        await status_message.edit_text(final_message, parse_mode=enums.ParseMode.HTML) 
    except Exception:
        await query.message.reply_text(final_message, parse_mode=enums.ParseMode.HTML) 


# ###################################################
# ## MAIN EXECUTION ##
# ###################################################

async def startup_task():
    """Runs immediately after the bot client successfully starts."""
    await send_startup_notification(app)

if __name__ == "__main__":
    logger.info("Starting Pyrogram bot...")
    
    # Set the startup function to run once the client is connected
    @app.on_raw_update()
    async def run_startup_task(client, update, users, chats):
        # This check prevents running the task multiple times
        if not hasattr(client, '_startup_done'):
            client._startup_done = True
            await startup_task()

    try:
        # The robust way to run a Pyrogram client
        app.run()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"An unexpected error occurred during bot execution: {e}")
