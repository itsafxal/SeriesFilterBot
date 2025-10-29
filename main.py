import re
import os
import asyncio
import logging
from pyrogram import Client, filters, enums # <<< ADDED enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from pymongo import MongoClient, TEXT

# --- Setup Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- CONFIGURATION ---
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
# ## PYROGRAM HANDLERS ##
# ###################################################

# --- Startup Logic (Now correctly async/await) ---
async def send_startup_notification(client):
    """Sends a notification to the owner when the bot starts successfully."""
    try:
        await client.send_message(
            chat_id=OWNER_ID,
            text="✨ <b>Bot Restarted!</b>\n\nFile filter service is online and ready.",
            parse_mode=enums.ParseMode.HTML # <<< FIXED
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
    await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML) # <<< FIXED

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
            button_text = name[:50] + '...' if len(name) > 50 else name
            keyboard.append([InlineKeyboardButton(button_text, callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"🔎 Found {len(unique_names)} matching series. Please select one:"
        await message.reply_text(response, reply_markup=reply_markup)
    else:
        # Changed formatting to HTML <code> tag
        await message.reply_text(f"❌ No series files found matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML) # <<< FIXED

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
            # Changed formatting to HTML <b> tag
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
        
        if len(row) >= 5: 
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    # Changed formatting to HTML <b> tag
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
        # Changed formatting to HTML <b> tag
        await query.message.reply_text(text=f"❌ No files found for <b>{series_name}</b> Season {season:02d}.", parse_mode=enums.ParseMode.HTML) # <<< FIXED
        return

    # Changed formatting to HTML <b> tag
    status_message = await query.message.reply_text(
        f"📦 Sending {len(files)} files for <b>{series_name}</b> Season {season:02d}..."
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
                await asyncio.sleep(0.5) 
            except Exception as e:
                logger.error(f"Failed to copy message {message_id} from {channel_id}: {e}")

    # Changed formatting to HTML <b> tag
    final_message = f"✅ Sent <b>{success_count}</b> files for <b>{series_name}</b> Season {season:02d}."
    
    try:
        await status_message.edit_text(final_message, parse_mode=enums.ParseMode.HTML) # <<< FIXED
    except Exception:
        await query.message.reply_text(final_message, parse_mode=enums.ParseMode.HTML) # <<< FIXED

# ###################################################
# ## INDEXING COMMAND (Pyrogram Client) ##
# ###################################################

@app.on_message(filters.command("index") & filters.private)
async def index_channel_files_command(client, message):
    """Command to start indexing files from the DUMP_CHANNEL_ID (Owner Only)."""
    if message.from_user.id != OWNER_ID:
        # Changed formatting to HTML <b> tag
        await message.reply_text("⛔️ You are <b>not authorized</b> to run this command.", parse_mode=enums.ParseMode.HTML) # <<< FIXED
        return
    
    # Changed formatting to HTML <b> tag and <code> tag
    status_message = await message.reply_text(
        f"⏳ Attempting to start indexer. This requires a <b>user session</b> to read channel history. Channel: <code>{DUMP_CHANNEL_ID}</code>"
    )
    
    indexer_app = Client("IndexerUser", api_id=API_ID, api_hash=API_HASH)

    try:
        await indexer_app.start()
        logger.info("Pyrogram User Client started for indexing.")
    except Exception as e:
        # Changed formatting to HTML <b> tag and <code> tag
        error_msg = f"❌ Failed to start Indexer Client for login. You need a valid user session. Error: <code>{e}</code>"
        await status_message.edit_text(error_msg, parse_mode=enums.ParseMode.HTML) # <<< FIXED
        logger.error(error_msg)
        return

    await clear_index() 
    # Changed formatting to HTML <b> tag
    await status_message.edit_text(f"Indexing... <b>Cleared old index</b>. Starting file processing.")

    total_indexed = await index_channel_files(indexer_app, status_message)
    
    await indexer_app.stop() 
    
    # Changed formatting to HTML <b> tag
    final_message = f"🎉 <b>Indexing Complete!</b>\n\nTotal files indexed: <b>{total_indexed}</b>."
    await status_message.edit_text(final_message, parse_mode=enums.ParseMode.HTML) # <<< FIXED


async def index_channel_files(indexer_client, status_message):
    """Iterates through a channel using Pyrogram and stores file metadata in MongoDB."""
    
    indexed_count = 0
    buffer = [] 
    
    try:
        async for msg in indexer_client.get_chat_history(DUMP_CHANNEL_ID): 
            
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
                        await index_files_in_db(buffer) 
                        indexed_count += len(buffer)
                        buffer = []
                        
                        # Changed formatting to HTML <b> tag
                        await status_message.edit_text(f"Indexing... <b>{indexed_count}</b> files processed so far.")
                
                await asyncio.sleep(0.05)
                
        if buffer:
            await index_files_in_db(buffer) 
            indexed_count += len(buffer)
            
    except Exception as e:
        # Changed formatting to HTML <b> tag and <code> tag
        await status_message.edit_text(f"❌ An unknown error occurred during indexing: <code>{e}</code>", parse_mode=enums.ParseMode.HTML) # <<< FIXED
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
    # This replaces the need for the custom main_bot_loop()
    app.add_handler(lambda _, client: asyncio.create_task(startup_task()))
    
    try:
        # The robust way to run a Pyrogram client, fixing the loop conflict
        app.run()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"An unexpected error occurred during bot execution: {e}")
