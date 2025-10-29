import re
import os
import asyncio
import logging
from pyrotgfork import Client, filters
from pyrotgfork.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrotgfork.errors import MessageNotModified
from motor.motor_asyncio import AsyncIOMotorClient 

# --- Setup Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
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

# --- PyroTGfork Client Setup ---
# Initialize the client for BOT operations. 
# We use in_memory=True to avoid creating a session file for the bot token.
app = Client(
    "SeriesFilterBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True # Avoids disk session files for the bot
)


# ###################################################
# ## DATABASE FUNCTIONS (Async Motor) ##
# ###################################################

async def search_series(query: str):
    """Searches MongoDB for unique series names matching the query."""
    regex_query = re.escape(query).replace(" ", ".*")
    return await files_collection.distinct(
        "normalized_name", 
        {"normalized_name": {"$regex": regex_query, "$options": "i"}}
    )

async def get_unique_seasons_by_name(series_name: str):
    """Gets unique season numbers for a given series."""
    seasons = await files_collection.distinct("season", {"normalized_name": series_name})
    return sorted([s for s in seasons if s > 0])

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
# ## PYROTGFORK HANDLERS ##
# ###################################################

async def send_restart_message(client):
    """Sends a notification to the owner when the bot starts successfully."""
    try:
        await client.send_message(
            chat_id=OWNER_ID,
            text="✨ <b>Bot Restarted!</b>\n\nFile filter service is online and ready.",
            parse_mode="html" 
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
    await message.reply_text(welcome_text, parse_mode='html')

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
            # PyroTGfork uses the Pyrogram format for buttons
            keyboard.append([InlineKeyboardButton(button_text, callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"🔎 Found {len(unique_names)} matching series. Please select one:"
        await message.reply_text(response, reply_markup=reply_markup)
    else:
        await message.reply_text(f"❌ No series files found matching '{query}'.")

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
            await query.message.edit_text(text=f"❌ No seasons found for {series_name}.")
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
    caption = f"📺 {series_name}\n\nSelect a Season:"
    
    try:
        await query.message.edit_text(caption, reply_markup=reply_markup)
    except MessageNotModified:
        pass


async def send_season_files(query, series_name: str, season: int):
    """Sends all files belonging to the selected season using copy_message."""
    user_chat_id = query.message.chat.id
    files = await get_all_files_by_season(series_name, season)
    
    if not files:
        await query.message.reply_text(text=f"❌ No files found for {series_name} Season {season:02d}.")
        return

    status_message = await query.message.reply_text(
        f"📦 Sending {len(files)} files for {series_name} Season {season:02d}..."
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

    final_message = f"✅ Sent {success_count} files for {series_name} Season {season:02d}."
    
    try:
        await status_message.edit_text(final_message)
    except Exception:
        await query.message.reply_text(final_message)

# ###################################################
# ## INDEXING COMMAND (PyroTGfork User Client) ##
# ###################################################

@app.on_message(filters.command("index") & filters.private)
async def index_channel_files_command(client, message):
    """Command to start indexing files from the DUMP_CHANNEL_ID (Owner Only)."""
    if message.from_user.id != OWNER_ID:
        await message.reply_text("⛔️ You are not authorized to run this command.")
        return
    
    # 1. Create a temporary client for indexing (requires user privileges)
    # ⚠️ THIS WILL LIKELY FAIL IN NON-INTERACTIVE ENVIRONMENTS! ⚠️
    # To fix this, you must initialize the indexer client with a session string:
    # indexer_app = Client(USER_SESSION_STRING, API_ID, API_HASH)
    indexer_app = Client("IndexerUser", api_id=API_ID, api_hash=API_HASH)

    status_message = await message.reply_text(
        f"⏳ Attempting to start indexer. **If this hangs, the user login failed.** Channel: {DUMP_CHANNEL_ID}"
    )
    
    # 2. Start the Indexer Client
    try:
        await indexer_app.start()
        logger.info("PyroTGfork User Client started for indexing.")
    except Exception as e:
        error_msg = f"❌ Failed to start Indexer Client for login: {e}"
        await status_message.edit_text(error_msg)
        logger.error(error_msg)
        return

    # 3. Clear the old index
    await clear_index() 
    await status_message.edit_text(f"Indexing... Cleared old index. Starting file processing.")

    # 4. Call the indexing function
    total_indexed = await index_channel_files(indexer_app, status_message)
    
    # 5. Stop the Indexer Client
    await indexer_app.stop() 
    
    final_message = f"🎉 Indexing Complete!\n\nTotal files indexed: {total_indexed}."
    await status_message.edit_text(final_message)


async def index_channel_files(indexer_client, status_message):
    """Iterates through a channel using PyroTGfork and stores file metadata in MongoDB."""
    
    indexed_count = 0
    buffer = [] 
    
    try:
        # Use the indexer client to read history
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
                        
                        await status_message.edit_text(f"Indexing... {indexed_count} files processed so far.")
                
                await asyncio.sleep(0.05)
                
        if buffer:
            await index_files_in_db(buffer)
            indexed_count += len(buffer)
            
    except Exception as e:
        await status_message.edit_text(f"❌ An unknown error occurred during indexing: {e}")
        logger.error(f"Indexing Error: {e}")
            
    return indexed_count


# ###################################################
# ## MAIN EXECUTION ##
# ###################################################

if __name__ == "__main__":
    logger.info("Starting PyroTGfork bot...")
    
    # Add a handler to run startup tasks (like sending the restart message)
    async def startup_tasks(_):
        await send_restart_message(app)

    # Use the app.run() entry point
    app.run(startup_tasks)
