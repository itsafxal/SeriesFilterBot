import re
import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified, BotMethodInvalid
from motor.motor_asyncio import AsyncIOMotorClient 

# --- CONFIGURATION ---
API_ID = 21352077 # Your API ID
API_HASH = "e4ba74d3f410dffbe848402989e61a04" # Your API Hash
BOT_TOKEN = "7661197159:AAHwppy1JjmEBXucY1gxdbfGKVpuqjl_gFg" # Your Bot Token
MONGO_URI = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0" # Your MongoDB connection string
DUMP_CHANNEL_ID = int(os.environ.get("DUMP_CHANNEL_ID", -1001996538290))
OWNER_ID = int(os.environ.get("OWNER_ID", 5483128891)) 

# --- Database Setup ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
files_collection = db["files"]

# Ensure a combined index for fast filtering and a text index for better searching
files_collection.create_index([("normalized_name", 1), ("season", 1)])
files_collection.create_index([("normalized_name", "text")])


# --- Pyrogram Clients ---
# 1. Bot Client (for message handling, inline buttons)
app = Client(
    "SeriesFilterBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# 2. User Client (for channel history reading, requires session login)
user_app = Client(
    "IndexerUser",
    api_id=API_ID,
    api_hash=API_HASH
)

# --- Utility Functions for File Name Parsing ---

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


# --- Database Interaction Functions ---

async def search_series(query: str):
    """Searches MongoDB for unique series names matching the query."""
    regex_query = re.escape(query).replace(" ", ".*")
    series_names = await files_collection.distinct("normalized_name", {"normalized_name": {"$regex": regex_query, "$options": "i"}})
    return series_names

async def get_unique_seasons_by_name(series_name: str):
    """Gets unique season numbers for a given series."""
    seasons = await files_collection.distinct("season", {"normalized_name": series_name})
    seasons = sorted([s for s in seasons if s > 0])
    return seasons

async def get_all_files_by_season(series_name: str, season: int):
    """Gets file details (channel_id, message_id) for a specific season."""
    files = await files_collection.find(
        {"normalized_name": series_name, "season": season},
        {"channel_id": 1, "message_id": 1, "_id": 0}
    ).to_list(length=None) 
    return files


# ###################################################
# ## ✨ NEW: Bot Startup and Start Command ##
# ###################################################

@app.on_message(filters.command("start") & filters.private)
async def start_command(_, message):
    """Handles the /start command and welcomes the user."""
    welcome_text = (
        "👋 **Welcome! I'm your Series File Filter Bot.**\n\n"
        "Just send me the **name of a series** you are looking for, "
        "and I'll show you the available seasons and episodes from the index.\n\n"
        "**Example:** `The Last of Us`\n\n"
        f"**Admin:** Use `/index` to update the file database."
    )
    await message.reply_text(welcome_text, parse_mode="markdown")

async def send_restart_message():
    """Sends a notification to the owner when the bot starts successfully."""
    try:
        await app.send_message(
            chat_id=OWNER_ID,
            text="✨ **Bot Restarted!**\n\nFile filter service is online and ready.",
            parse_mode="markdown"
        )
        print(f"Sent restart message to owner {OWNER_ID}.")
    except Exception as e:
        print(f"Failed to send restart message to owner {OWNER_ID}: {e}")
        
# ###################################################
# ## 🔎 Autofilter Logic Handlers ##
# ###################################################

@app.on_message(filters.text & filters.private & ~filters.command(["start", "index"]))
async def search_handler(_, message):
    """Handles user text search queries and initiates the series selection menu."""
    query = message.text.strip()
    
    unique_names = await search_series(query)
    
    if unique_names:
        keyboard = []
        for name in sorted(unique_names):
            callback_data = f'series_select|{name}'
            button_text = name[:50] + '...' if len(name) > 50 else name
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"🔎 Found {len(unique_names)} matching series. Please select one:"
        await message.reply_text(response, reply_markup=reply_markup, parse_mode=None)
    else:
        await message.reply_text(f"❌ No series files found matching '{query}'.", parse_mode=None)


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
        print(f"Error in callback: {e}")
        await query.answer("An internal error occurred. Try again.", show_alert=True)
    
    await query.answer()

async def show_season_menu(query, series_name):
    """Displays buttons for all unique seasons of the selected series."""
    seasons = await get_unique_seasons_by_name(series_name)
    
    if not seasons:
        try:
            await query.message.edit_text(text=f"❌ No seasons found for {series_name}.", parse_mode=None)
        except MessageNotModified:
            pass
        return

    keyboard = []
    row = []
    for season in seasons:
        callback_data = f'season_send|{series_name}|{season}'
        button_text = f"S{season:02d}"
        row.append(InlineKeyboardButton(button_text, callback_data=callback_data))
        
        if len(row) >= 5: 
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    caption = f"📺 {series_name}\n\nSelect a Season:"
    
    try:
        await query.message.edit_text(
            text=caption,
            reply_markup=reply_markup,
            parse_mode=None
        )
    except MessageNotModified:
        pass


async def send_season_files(query, series_name, season):
    """Sends all files belonging to the selected season using copy_message."""
    user_chat_id = query.message.chat.id
    files = await get_all_files_by_season(series_name, season)
    
    if not files:
        await query.message.reply_text(text=f"❌ No files found for {series_name} Season {season:02d}.", parse_mode=None)
        return

    status_message = await query.message.reply_text(
        f"📦 Sending {len(files)} files for {series_name} Season {season:02d}...",
        parse_mode=None
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
                await asyncio.sleep(0.5) # Flood limit buffer
            except Exception as e:
                print(f"Failed to copy message {message_id} from {channel_id}: {e}")

    final_message = f"✅ Sent {success_count} files for {series_name} Season {season:02d}."
    
    try:
        await status_message.edit_text(final_message, parse_mode=None)
    except Exception:
        await query.message.reply_text(final_message, parse_mode=None)

# ###################################################
# ## 💾 Indexing Command ##
# ###################################################

@app.on_message(filters.command("index") & filters.private)
async def index_channel_files_command(_, message):
    """Command to start indexing files from the DUMP_CHANNEL_ID (Owner Only)."""
    if message.from_user.id != OWNER_ID:
        await message.reply_text("⛔️ You are not authorized to run this command.", parse_mode=None)
        return
        
    status_message = await message.reply_text(f"⏳ Starting index of files from channel {DUMP_CHANNEL_ID}. This may take a while...", parse_mode=None)
    
    # 1. Start the User Client before the indexing loop
    try:
        await user_app.start()
    except Exception as e:
        error_msg = f"❌ Failed to start User Client for indexing. Check session file/login: {e}"
        await status_message.edit_text(error_msg, parse_mode=None)
        print(error_msg)
        return

    # Confirm clearing the old index
    await files_collection.delete_many({}) 
    try:
        await status_message.edit_text(f"Indexing... Cleared old index. Starting file processing.", parse_mode=None)
    except MessageNotModified:
        pass

    # 2. Pass the user_app to the indexing function
    total_indexed = await index_channel_files(status_message)
    
    # 3. Stop the User Client after indexing is complete
    await user_app.stop() 
    
    final_message = f"🎉 Indexing Complete!\n\nTotal files indexed: {total_indexed}."
    await status_message.edit_text(final_message, parse_mode=None)


async def index_channel_files(status_message):
    """Iterates through a channel and stores file metadata in MongoDB. Uses the global user_app."""
    
    indexed_count = 0
    
    try:
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
                    
                    await files_collection.insert_one(file_doc)
                    indexed_count += 1
                    
                    if indexed_count % 50 == 0:
                        try:
                            await status_message.edit_text(f"Indexing... {indexed_count} files processed so far.", parse_mode=None)
                        except MessageNotModified:
                            pass
                
                await asyncio.sleep(0.1) 
                
    except Exception as e:
        await status_message.edit_text(f"❌ An unknown error occurred during indexing: {e}", parse_mode=None)
        print(f"Indexing Error: {e}")
        return 0
            
    return indexed_count

# --- Run the Bot ---
if __name__ == "__main__":
    print("Bot started...")
    
    # Use Pyrogram's `idle` to run a coroutine (the restart message) before blocking
    async def main():
        # Start the Bot Client
        await app.start()
        
        # Send the restart notification to the admin
        await send_restart_message()
        
        # Keep the bot running
        await app.idle()
        
        # Stop the client when idle is exited (e.g., Ctrl+C)
        await app.stop()

    # Pyrogram must be run using asyncio.run() or app.run(). 
    # For a coroutine like `main`, we use the standard Python asyncio run.
    asyncio.run(main())
