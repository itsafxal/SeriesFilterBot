# bot_main.py (The full code for your Series Filter Bot)

import re
import os
import asyncio
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from pymongo import MongoClient, TEXT

# --- Import Configuration ---
from config import (
    API_ID, API_HASH, BOT_TOKEN, MONGO_URI, DUMP_CHANNEL_ID, OWNER_ID, INDEXER_SESSION_NAME
)

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


# ###################################################
# ## DATABASE WRAPPER FUNCTIONS (PyMongo via asyncio.to_thread) ##
# ###################################################

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
    
async def index_files_in_db(file_docs):
    """Inserts multiple file documents into MongoDB."""
    def sync_insert_many():
        if file_docs:
            files_collection.insert_many(file_docs)
            return len(file_docs)
        return 0
        
    return await asyncio.to_thread(sync_insert_many)

async def clear_index():
    """Clears all documents from the collection."""
    def sync_delete():
        files_collection.delete_many({})
        
    await asyncio.to_thread(sync_delete)

# ###################################################
# ## UTILITY & PARSING ##
# ###################################################

def parse_series_info(file_name: str):
    """
    Attempts to extract Series Name, Season (SXX), and Episode (EXX) from a filename.
    """
    if not file_name:
        return "", 0, 0
        
    # Regex to find SXXEXX pattern
    match = re.search(r"[sS](\d+)[eE](\d+)", file_name)
    
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        
        # Remove the SXXEXX part and surrounding separators
        normalized_name = re.sub(r"([. _-]+)?[sS]\d+[eE]\d+([. _-]+)?", " ", file_name).strip()
        
        # Clean common garbage terms (case-insensitive)
        for term in ["4K", "1080P", "720P", "HDRIP", "WEB-DL", "AMZN", "NETFLIX", "DDP5.1", "X265", "x264", "MULTISUB", "AAC"]:
            # Use \b for word boundaries to avoid matching parts of longer words
            normalized_name = re.sub(r"\b" + re.escape(term) + r"\b", "", normalized_name, flags=re.IGNORECASE).strip()

        # Replace multiple spaces/dots/underscores with a single space, then Title case it
        normalized_name = re.sub(r"[. _]+", " ", normalized_name).title().strip()
        
        return normalized_name, season, episode
    
    # If SXXEXX is not found, treat it as a potential standalone file, with Season/Episode 0
    return file_name.title().strip(), 0, 0 


# ###################################################
# ## PYROGRAM HANDLERS ##
# ###################################################

# --- Startup Logic (Runs once after client connects) ---
async def send_startup_notification(client):
    """Sends a notification to the owner when the bot starts successfully."""
    try:
        await client.send_message(
            chat_id=OWNER_ID,
            text="✨ <b>Bot Restarted!</b>\n\nFile filter service is online and ready.",
            parse_mode=enums.ParseMode.HTML
        )
        logger.info("Sent restart message to owner.")
    except Exception as e:
        logger.error(f"Failed to send restart message to owner: {e}")

# Handler for the /start command
@app.on_message(filters.command("start") & filters.private)
async def start_command(_, message):
    """Handles the /start command."""
    welcome_text = (
        "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
        "Just send me the <b>name of a series</b> you are looking for, "
        "and I'll show you the available seasons and episodes from the index.\n\n"
        "<b>Example:</b> <code>The Last of Us</code>\n\n"
        f"<b>Admin:</b> Use <code>/index</code> to update the file database."
    )
    await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

# Handler for plain text messages in private chat
@app.on_message(filters.text & filters.private & ~filters.command(["start", "index"]))
async def search_handler(_, message):
    """Handles user text search queries."""
    query = message.text.strip()
    
    unique_names = await search_series(query) 
    
    if unique_names:
        keyboard = []
        for name in sorted(unique_names):
            callback_data = f'series_select|{name}'
            # Truncate long names for button text
            button_text = name[:50] + '...' if len(name) > 50 else name
            keyboard.append([InlineKeyboardButton(button_text, callback_data)])
        
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

    keyboard = []
    row = []
    for season in seasons:
        callback_data = f'season_send|{series_name}|{season}'
        button_text = f"S{season:02d}"
        row.append(InlineKeyboardButton(button_text, callback_data))
        
        if len(row) >= 5: # 5 buttons per row for better layout
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = f"📺 <b>{series_name}</b>\n\nSelect a Season:"
    
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
# ## INDEXING COMMAND (Pyrogram Client) ##
# ###################################################

@app.on_message(filters.command("index") & filters.private)
async def index_channel_files_command(client, message):
    """Command to start indexing files from the DUMP_CHANNEL_ID (Owner Only)."""
    if message.from_user.id != OWNER_ID:
        await message.reply_text("⛔️ You are <b>not authorized</b> to run this command.", parse_mode=enums.ParseMode.HTML) 
        return
    
    status_message = await message.reply_text(
        f"⏳ Attempting to start indexer. This requires a <b>user session</b> to read channel history. Session name: <code>{INDEXER_SESSION_NAME}</code>"
    )
    
    # Initialize the user client with the defined session name
    indexer_app = Client(INDEXER_SESSION_NAME, api_id=API_ID, api_hash=API_HASH)

    try:
        # This requires IndexerUser.session to be present and valid
        await indexer_app.start() 
        logger.info("Pyrogram User Client started for indexing.")
    except Exception as e:
        error_msg = f"❌ Failed to start Indexer Client. Ensure 'IndexerUser.session' is present and valid. Error: <code>{e}</code>"
        await status_message.edit_text(error_msg, parse_mode=enums.ParseMode.HTML) 
        logger.error(error_msg)
        return

    await clear_index() 
    await status_message.edit_text(f"Indexing... <b>Cleared old index</b>. Starting file processing.")

    total_indexed = await index_channel_files(indexer_app, status_message)
    
    await indexer_app.stop() # Stop the user client after indexing is done
    
    final_message = f"🎉 <b>Indexing Complete!</b>\n\nTotal files indexed: <b>{total_indexed}</b>."
    await status_message.edit_text(final_message, parse_mode=enums.ParseMode.HTML) 


async def index_channel_files(indexer_client, status_message):
    """Iterates through a channel using Pyrogram and stores file metadata in MongoDB."""
    
    indexed_count = 0
    buffer = [] # Buffer for bulk inserts
    
    try:
        # Use the user client to get chat history, which handles private channels better
        async for msg in indexer_client.get_chat_history(DUMP_CHANNEL_ID): 
            
            if msg.document or msg.video:
                file_name = msg.document.file_name if msg.document else (msg.video.file_name or "")
                
                normalized_name, season, episode = parse_series_info(file_name)
                
                # Only index files that were successfully parsed as part of a series (Season > 0)
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
                    
                    # Bulk insert to reduce database overhead
                    if len(buffer) >= 50:
                        await index_files_in_db(buffer) 
                        indexed_count += len(buffer)
                        buffer = []
                        
                        # Update status every 50 files
                        await status_message.edit_text(f"Indexing... <b>{indexed_count}</b> files processed so far.")
                
                # Small sleep to prevent flood waits from Telegram API
                await asyncio.sleep(0.05)
                
        # Insert any remaining files in the buffer
        if buffer:
            await index_files_in_db(buffer) 
            indexed_count += len(buffer)
            
    except Exception as e:
        await status_message.edit_text(f"❌ An unknown error occurred during indexing: <code>{e}</code>", parse_mode=enums.ParseMode.HTML) 
        logger.error(f"Indexing Error: {e}")
            
    return indexed_count


# ###################################################
# ## MAIN EXECUTION ##
# ###################################################

async def startup_task():
    """Runs immediately after the bot client successfully starts."""
    await send_startup_notification(app)

if __name__ == "__main__":
    logger.info("Starting Pyrogram bot...")
    
    # Set the startup function to run once the client is connected
    # We use a handler since it's the standard Pyrogram way to run async code after startup
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
