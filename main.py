import re
import os
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from pymongo import MongoClient, TEXT

# --- Setup Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- CONFIGURATION ---
# Note: It's better practice to load secrets from environment variables
API_ID = 21352077
API_HASH = "e4ba74d3f410dffbe848402989e61a04"
BOT_TOKEN = "8163888122:AAEuGzXZDxKlcehE2b5Yj4kpayBUo1VaZWs"
MONGO_URI = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DUMP_CHANNEL_ID = int(os.environ.get("DUMP_CHANNEL_ID", -1001996538290))
OWNER_ID = int(os.environ.get("OWNER_ID", 5483128891))

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
        # Escape the query for safe regex use, then replace spaces with '.*' for flexibility
        regex_query = re.escape(query).replace(" ", ".*")
        pipeline = [
            # Case-insensitive regex search
            {"$match": {"normalized_name": {"$regex": regex_query, "$options": "i"}}}, 
            # Group by normalized name to get unique series
            {"$group": {"_id": "$normalized_name"}},
            # Project to rename _id back to 'name'
            {"$project": {"_id": 0, "name": "$_id"}}
        ]
        return [doc["name"] for doc in files_collection.aggregate(pipeline)]
        
    return await asyncio.to_thread(sync_search)

async def get_unique_seasons_by_name(series_name: str):
    """Gets unique season numbers for a given series."""
    def sync_get_seasons():
        seasons = files_collection.distinct("season", {"normalized_name": series_name})
        # Filter out season 0 (non-series files) and sort them
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
        
        # Remove SXXEXX and any surrounding separators
        normalized_name = re.sub(r"([. _-]+)?[sS]\d+[eE]\d+([. _-]+)?", " ", file_name).strip()
        
        # Remove common quality/source tags (case insensitive)
        for term in ["4K", "1080P", "720P", "HDRIP", "WEB-DL", "AMZN", "NETFLIX", "DDP5.1", "X265", "x264", "MULTISUB", "AAC"]:
            normalized_name = re.sub(r"\b" + re.escape(term) + r"\b", "", normalized_name, flags=re.IGNORECASE).strip()

        # Replace multiple separators with a single space and title-case the name
        normalized_name = re.sub(r"[. _]+", " ", normalized_name).title().strip()
        
        return normalized_name, season, episode
    
    # If no SXXEXX pattern is found, return original name and season/episode 0
    return file_name.title().strip(), 0, 0 


# ###################################################
# ## PYROGRAM HANDLERS ##
# ###################################################

# Fix: Use on_message handler for bot startup notification (runs once)
@app.on_message(filters.regex("seriesfilterbot_startup_hook") & filters.chat(OWNER_ID))
async def send_restart_message(_):
    """Sends a notification to the owner when the bot starts successfully."""
    try:
        # Note: This handler should be triggered manually right after app.start()
        # in the main execution block for a single run. Since we are using app.run(), 
        # we will use an alternative simple function call right after the client is started.
        pass # The logic is now in the on_start method below
    except Exception as e:
        logger.error(f"Failed to send restart message to owner: {e}")

# NEW: Use Pyrogram's client.one_message_handler() or a simple function call 
# after app starts. Since app.run() is synchronous, we use a simple function 
# that can be called right at the start of the script before the bot starts.
# Or, let's keep the core logic and adapt the startup call.

# --- Corrected Startup Logic (Runs right after client connects) ---
async def send_startup_notification(client):
    """Sends a notification to the owner when the bot starts successfully."""
    try:
        await client.send_message(
            chat_id=OWNER_ID,
            text="✨ **Bot Restarted!**\n\nFile filter service is online and ready.",
            parse_mode="markdown" 
        )
        logger.info("Sent restart message to owner.")
    except Exception as e:
        logger.error(f"Failed to send restart message to owner: {e}")

# Handler for the /start command
@app.on_message(filters.command("start") & filters.private)
async def start_command(_, message):
    """Handles the /start command."""
    welcome_text = (
        "👋 **Welcome! I'm your Series File Filter Bot.**\n\n"
        "Just send me the **name of a series** you are looking for, "
        "and I'll show you the available seasons and episodes from the index.\n\n"
        "**Example:** `The Last of Us`\n\n"
        f"**Admin:** Use `/index` to update the file database."
    )
    await message.reply_text(welcome_text, parse_mode='markdown')

# Handler for plain text messages in private chat
@app.on_message(filters.text & filters.private & ~filters.command(["start", "index"]))
async def search_handler(_, message):
    """Handles user text search queries."""
    query = message.text.strip()
    
    unique_names = await search_series(query) # <<< Awaiting PyMongo wrapper
    
    if unique_names:
        keyboard = []
        for name in sorted(unique_names):
            callback_data = f'series_select|{name}'
            button_text = name[:50] + '...' if len(name) > 50 else name
            keyboard.append([InlineKeyboardButton(button_text, callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"🔎 Found {len(unique_names)} matching series. Please select one:"
        await message.reply_text(response, reply_markup=reply_markup)
    else:
        await message.reply_text(f"❌ No series files found matching '`{query}`'.", parse_mode='markdown')

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
    
    # Always answer the callback query to remove the loading state
    await query.answer()

async def show_season_menu(query, series_name: str):
    """Displays buttons for all unique seasons of the selected series."""
    seasons = await get_unique_seasons_by_name(series_name) # <<< Awaiting PyMongo wrapper
    
    if not seasons:
        try:
            await query.message.edit_text(text=f"❌ No seasons found for **{series_name}**.", parse_mode='markdown')
        except MessageNotModified:
            pass
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
    caption = f"📺 **{series_name}**\n\nSelect a Season:"
    
    try:
        await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode='markdown')
    except MessageNotModified:
        pass


async def send_season_files(query, series_name: str, season: int):
    """Sends all files belonging to the selected season using copy_message."""
    user_chat_id = query.message.chat.id
    files = await get_all_files_by_season(series_name, season) # <<< Awaiting PyMongo wrapper
    
    if not files:
        await query.message.reply_text(text=f"❌ No files found for **{series_name}** Season {season:02d}.", parse_mode='markdown')
        return

    status_message = await query.message.reply_text(
        f"📦 Sending {len(files)} files for **{series_name}** Season {season:02d}..."
    )

    success_count = 0
    
    for file_entry in files:
        channel_id = file_entry.get('channel_id')
        message_id = file_entry.get('message_id')
        
        if channel_id and message_id:
            try:
                await app.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id
                )
                success_count += 1
                await asyncio.sleep(0.5) # Throttle to prevent flooding Telegram
            except Exception as e:
                logger.error(f"Failed to copy message {message_id} from {channel_id}: {e}")

    final_message = f"✅ Sent **{success_count}** files for **{series_name}** Season {season:02d}."
    
    try:
        await status_message.edit_text(final_message, parse_mode='markdown')
    except Exception:
        # Fallback if status message can't be edited
        await query.message.reply_text(final_message, parse_mode='markdown')

# ###################################################
# ## INDEXING COMMAND (Pyrogram Client) ##
# ###################################################

@app.on_message(filters.command("index") & filters.private)
async def index_channel_files_command(client, message):
    """Command to start indexing files from the DUMP_CHANNEL_ID (Owner Only)."""
    if message.from_user.id != OWNER_ID:
        await message.reply_text("⛔️ You are **not authorized** to run this command.", parse_mode='markdown')
        return
    
    status_message = await message.reply_text(
        f"⏳ Attempting to start indexer. This requires a **user session** to read channel history. Channel: `{DUMP_CHANNEL_ID}`"
    )
    
    # 1. Create a temporary client for indexing (requires user privileges)
    # This Client is created *outside* of the main bot's event loop, which 
    # is crucial but sometimes tricky.
    indexer_app = Client("IndexerUser", api_id=API_ID, api_hash=API_HASH)

    # 2. Start the Indexer Client
    try:
        await indexer_app.start()
        logger.info("Pyrogram User Client started for indexing.")
    except Exception as e:
        error_msg = f"❌ Failed to start Indexer Client for login. You need a valid user session. Error: `{e}`"
        await status_message.edit_text(error_msg, parse_mode='markdown')
        logger.error(error_msg)
        return

    # 3. Clear the old index
    await clear_index() # <<< Awaiting PyMongo wrapper
    await status_message.edit_text(f"Indexing... **Cleared old index**. Starting file processing.")

    # 4. Call the indexing function
    total_indexed = await index_channel_files(indexer_app, status_message)
    
    # 5. Stop the Indexer Client
    await indexer_app.stop() 
    
    final_message = f"🎉 **Indexing Complete!**\n\nTotal files indexed: **{total_indexed}**."
    await status_message.edit_text(final_message, parse_mode='markdown')


async def index_channel_files(indexer_client, status_message):
    """Iterates through a channel using Pyrogram and stores file metadata in MongoDB."""
    
    indexed_count = 0
    buffer = [] 
    
    try:
        # Use the indexer client to read history
        # iterate_messages is more memory-efficient but get_chat_history is fine for smaller channels
        async for msg in indexer_client.get_chat_history(DUMP_CHANNEL_ID): 
            
            # Filter for documents or videos
            if msg.document or msg.video:
                file_name = msg.document.file_name if msg.document else (msg.video.file_name or "")
                
                normalized_name, season, episode = parse_series_info(file_name)
                
                if season > 0: # Only index files with a valid SXXEXX pattern
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
                    
                    # Batch insert for efficiency
                    if len(buffer) >= 50:
                        await index_files_in_db(buffer) # <<< Awaiting PyMongo wrapper
                        indexed_count += len(buffer)
                        buffer = []
                        
                        await status_message.edit_text(f"Indexing... **{indexed_count}** files processed so far.")
                
                # Small pause to respect API limits
                await asyncio.sleep(0.05)
                
        # Insert any remaining files in the buffer
        if buffer:
            await index_files_in_db(buffer) # <<< Awaiting PyMongo wrapper
            indexed_count += len(buffer)
            
    except Exception as e:
        await status_message.edit_text(f"❌ An unknown error occurred during indexing: `{e}`", parse_mode='markdown')
        logger.error(f"Indexing Error: {e}")
            
    return indexed_count


# ###################################################
# ## MAIN EXECUTION (Robust Fix) ##
# ###################################################

async def startup_task():
    """Runs immediately after the bot client successfully starts."""
    await send_startup_notification(app)

if __name__ == "__main__":
    logger.info("Starting Pyrogram bot...")
    
    # Set the startup function to run once the client is connected
    app.add_handler(lambda _, client: asyncio.create_task(startup_task()))
    
    try:
        # The robust way to run a Pyrogram client, fixing the loop conflict
        app.run()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"An unexpected error occurred during bot execution: {e}")
