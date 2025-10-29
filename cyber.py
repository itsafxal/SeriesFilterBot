import re
import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from motor.motor_asyncio import AsyncIOMotorClient # Recommended for async MongoDB

# --- Configuration (Replace with your actual values) ---
# It's best practice to use environment variables for sensitive data
API_ID = 1234567 # Your API ID
API_HASH = "YOUR_API_HASH" # Your API Hash
BOT_TOKEN = "YOUR_BOT_TOKEN" # Your Bot Token
MONGO_URI = "mongodb://localhost:27017/" # Your MongoDB connection string
DUMP_CHANNEL_ID = -1001234567890 # ID of the channel containing the files (must be a supergroup/channel ID)

# --- Database Setup ---
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
files_collection = db["files"]

# Ensure an index for efficient filtering
files_collection.create_index([("normalized_name", 1), ("season", 1)])

# --- Pyrogram Client ---
app = Client(
    "SeriesFilterBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Utility Functions for File Name Parsing ---

def parse_series_info(file_name):
    """
    Attempts to extract Series Name, Season (SXX), and Episode (EXX) from a filename.
    Returns: (normalized_name, season_number, episode_number)
    """
    # Pattern to find SXXEXX (e.g., S01E05) or SXX (e.g., S01)
    match = re.search(r"[sS](\d+)[eE](\d+)", file_name)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        
        # Remove the SXXEXX part and common separators for normalization
        normalized_name = re.sub(r"([. _-]+)?[sS]\d+[eE]\d+([. _-]+)?", " ", file_name).strip()
        normalized_name = re.sub(r"\W+", " ", normalized_name).title().strip()
        
        # Simple cleanup (add more as needed)
        for term in ["4K", "1080P", "720P", "HDRIP", "WEB-DL", "AMZN", "NETFLIX", "DDP5.1", "X265", "x264"]:
            normalized_name = re.sub(r"\b" + re.escape(term) + r"\b", "", normalized_name, flags=re.IGNORECASE).strip()
            
        return normalized_name, season, episode
    
    # Simple fallback if the SXXEXX pattern is not found
    return file_name.title().strip(), 0, 0 # Season 0 for non-standard files


# --- Database Interaction Functions ---

async def search_series(query: str):
    """Searches MongoDB for unique series names matching the query."""
    # Simple search, more advanced indexing/full-text search is better for production
    regex_query = re.escape(query).replace(" ", ".*")
    
    # Find all unique series names that contain the query
    cursor = files_collection.find(
        {"normalized_name": {"$regex": regex_query, "$options": "i"}},
        {"normalized_name": 1}
    ).distinct("normalized_name")
    
    return await cursor # returns a list of unique names

async def get_unique_seasons_by_name(series_name: str):
    """Gets unique season numbers for a given series."""
    # Find all unique season numbers for the series
    cursor = files_collection.find(
        {"normalized_name": series_name},
        {"season": 1}
    ).distinct("season")
    
    seasons = sorted([s for s in await cursor if s > 0]) # Filter out season 0 and sort
    return seasons

async def get_all_files_by_season(series_name: str, season: int):
    """Gets file details (channel_id, message_id) for a specific season."""
    files = files_collection.find(
        {"normalized_name": series_name, "season": season},
        {"channel_id": 1, "message_id": 1, "_id": 0}
    ).to_list(length=None)
    
    return files

# --- Pyrogram Handlers (Autofilter Logic) ---

@app.on_message(filters.text & filters.private & ~filters.command)
async def search_handler(_, message):
    """Handles user text search queries and initiates the series selection menu."""
    query = message.text.strip()
    
    # 1. Search for matching series names
    unique_names = await search_series(query)
    
    if unique_names:
        keyboard = []
        for name in sorted(unique_names): # Sort for consistent display
            callback_data = f'series_select|{name}'
            # Use short button text if the name is very long
            button_text = name[:50] + '...' if len(name) > 50 else name
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"**🔎 Found {len(unique_names)} matching series.** Please select one:"
        await message.reply_text(response, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await message.reply_text(f"❌ No series files found matching '**{query}**'.")


@app.on_callback_query(filters.regex(r"series_select|season_send"))
async def callback_query_handler(_, query):
    """Handles button clicks for series and season selection."""
    data = query.data.split('|')
    action_type = data[0]
    
    if action_type == 'series_select':
        series_name = data[1]
        await show_season_menu(query, series_name)
        
    elif action_type == 'season_send':
        series_name = data[1]
        season = int(data[2])
        await send_season_files(query, series_name)
    
    await query.answer() # Always answer the query to dismiss the loading

async def show_season_menu(query, series_name):
    """Displays buttons for all unique seasons of the selected series."""
    seasons = await get_unique_seasons_by_name(series_name)
    
    if not seasons:
        await query.edit_message_text(text=f"❌ No seasons found for **{series_name}**.", parse_mode='Markdown')
        return

    keyboard = []
    row = []
    for season in seasons:
        callback_data = f'season_send|{series_name}|{season}'
        button_text = f"S{season:02d}"
        row.append(InlineKeyboardButton(button_text, callback_data=callback_data))
        
        if len(row) >= 5: # 5 buttons per row
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    caption = f"📺 **{series_name}**\n\n**Select a Season:**"
    
    try:
        await query.edit_message_text(
            text=caption,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except MessageNotModified:
        pass # Ignore if the message is already the same


async def send_season_files(query, series_name):
    """Sends all files belonging to the selected season using copy_message."""
    user_chat_id = query.message.chat.id
    season = int(query.data.split('|')[2])
    files = await get_all_files_by_season(series_name, season)
    
    if not files:
        await query.edit_message_text(text=f"❌ No files found for **{series_name}** Season **{season:02d}**.", parse_mode='Markdown')
        return

    status_message = await query.message.reply_text(
        f"📦 Sending **{len(files)} files** for **{series_name}** Season **{season:02d}**...",
        parse_mode='Markdown'
    )

    success_count = 0
    for file_entry in files:
        channel_id = file_entry.get('channel_id')
        message_id = file_entry.get('message_id')
        
        if channel_id and message_id:
            try:
                # Use query.message.client (the Pyrogram client instance)
                await query.message.client.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id
                )
                success_count += 1
                await asyncio.sleep(0.5) # Be kind to Telegram's flood limits
            except Exception as e:
                print(f"Failed to copy message {message_id} from {channel_id}: {e}")

    final_message = f"✅ Sent **{success_count}** files for **{series_name}** Season **{season:02d}**."
    
    try:
        await status_message.edit_text(final_message, parse_mode='Markdown')
    except Exception:
        await query.message.reply_text(final_message, parse_mode='Markdown')


# --- Indexing Command ---

@app.on_message(filters.command("index") & filters.private)
async def index_channel_files_command(_, message):
    """Command to start indexing files from the DUMP_CHANNEL_ID."""
    if message.from_user.id != (await app.get_me()).id: # Simple owner check (replace with a proper admin check)
        await message.reply_text("⛔️ You are not authorized to run this command.")
        return
        
    status_message = await message.reply_text(f"⏳ **Starting index of files** from channel `{DUMP_CHANNEL_ID}`. This may take a while...")
    
    total_indexed = await index_channel_files(message.client, status_message)
    
    final_message = f"🎉 **Indexing Complete!**\n\n**Total files indexed:** `{total_indexed}`."
    await status_message.edit_text(final_message, parse_mode='Markdown')


async def index_channel_files(client, status_message):
    """Iterates through a channel and stores file metadata in MongoDB."""
    
    print(f"Starting index for channel: {DUMP_CHANNEL_ID}")
    await files_collection.delete_many({}) # Clear old index for a fresh start (optional)
    
    indexed_count = 0
    async for msg in client.get_chat_history(DUMP_CHANNEL_ID):
        # Check if the message contains a document or video (you can expand this)
        if msg.document or msg.video:
            file_name = msg.document.file_name if msg.document else msg.video.file_name
            file_name = file_name if file_name else ""
            
            normalized_name, season, episode = parse_series_info(file_name)
            
            # Only index if parsing was successful and it looks like a series/episode
            if season > 0 and episode > 0:
                # Create the document to insert
                file_doc = {
                    "channel_id": DUMP_CHANNEL_ID,
                    "message_id": msg.id,
                    "file_id": msg.document.file_id if msg.document else msg.video.file_id,
                    "file_name": file_name,
                    "normalized_name": normalized_name,
                    "season": season,
                    "episode": episode,
                    "timestamp": msg.date
                }
                
                # Insert or Update (upsert is good for not re-indexing the same file)
                await files_collection.update_one(
                    {"channel_id": DUMP_CHANNEL_ID, "message_id": msg.id}, # Unique key
                    {"$set": file_doc},
                    upsert=True
                )
                indexed_count += 1
                
                # Update status message periodically
                if indexed_count % 50 == 0:
                    try:
                        await status_message.edit_text(f"Indexing... **{indexed_count} files** processed so far.")
                    except MessageNotModified:
                        pass
                    
            await asyncio.sleep(0.1) # Wait a bit to avoid flood limits
            
    return indexed_count

# --- Run the Bot ---
if __name__ == "__main__":
    print("Bot started...")
    app.run()
