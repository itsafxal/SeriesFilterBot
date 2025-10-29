# bot_main.py (Complete Code for Real-time Series Indexing Bot - Final Version)

import re
import os
import asyncio
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from pymongo import MongoClient, TEXT, DESCENDING 

# --- Configuration Placeholders (Your actual values) ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "7661197159:AAFfQ-d7k89NYDK3N64yoYzh_HJzQ0P52UU"
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
try:
    # 1. Compound index for fast unique checks and sorting
    files_collection.create_index([("normalized_name", 1), ("season", 1)], background=True)
    
    # 2. Ensure the required TEXT INDEX for fuzzy search is created
    files_collection.create_index(
        [("normalized_name", TEXT)], 
        name="series_name_text_index", 
        default_language='english',
        background=True, 
        unique=False
    )
    logger.info("✅ MongoDB Indexes ensured/created successfully.")
except Exception as e:
    logger.error(f"❌ Error creating MongoDB Indexes. The bot may fail to search: {e}")


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
    """
    Searches MongoDB for unique series names matching the query. 
    Prioritizes text index for better fuzzy matching.
    """
    def sync_search():
        # 1. Text Search 
        pipeline_text_search = [
            {"$match": {"$text": {"$search": query}}},
            {"$project": {
                "normalized_name": 1,
                "search_score": {"$meta": "textScore"} 
            }},
            {"$sort": {"search_score": DESCENDING}}, 
            {"$group": {"_id": "$normalized_name"}},
            {"$project": {"_id": 0, "name": "$_id"}}
        ]
        
        try:
            results = list(files_collection.aggregate(pipeline_text_search))
            if results:
                return results
        except Exception as e:
            logger.warning(f"Text search failed (Index issue likely): {e}. Falling back to Regex.")
        
        # 2. Fallback to the original regex search
        regex_query = re.escape(query).replace(" ", ".*") 
        pipeline_regex_search = [
            {"$match": {"normalized_name": {"$regex": regex_query, "$options": "i"}}},
            {"$group": {"_id": "$normalized_name"}},
            {"$project": {"_id": 0, "name": "$_id"}}
        ]
        return list(files_collection.aggregate(pipeline_regex_search))
        
    results = await asyncio.to_thread(sync_search)
    return [doc["name"] for doc in results]

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
        ).sort("episode", 1)
        return list(files)
        
    return await asyncio.to_thread(sync_get_files)
    
async def save_file_to_db(file_doc: dict):
    """Inserts a single file document into MongoDB."""
    def sync_insert_one():
        try:
            result = files_collection.update_one(
                {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
                {"$set": file_doc},
                upsert=True
            )
            
            if result.upserted_id:
                logger.info(f"DB: INSERTED new file. Msg ID: {file_doc['message_id']}")
            elif result.modified_count > 0:
                logger.info(f"DB: UPDATED existing file. Msg ID: {file_doc['message_id']}")
            else:
                 logger.debug(f"DB: SKIPPED (Duplicate unchanged). Msg ID: {file_doc['message_id']}")

            return True
        except Exception as e:
            logger.error(f"Failed to insert/update file doc: {e}")
            return False
            
    return await asyncio.to_thread(sync_insert_one)

###################################################
## UTILITY & PARSING (Name Unification Logic) ##
###################################################

def parse_series_info(file_name: str):
    """
    Attempts to extract Series Name, Season (SXX), and Episode (EXX) from a filename.
    Includes enhanced cleaning logic to ensure a pure series name.
    """
    if not file_name:
        return "", 0, 0
        
    # FIX APPLIED HERE: Added separator group ([. _-]) between SXX and EXX
    # This matches patterns like S01E01, S01.E01, S01_E01
    match = re.search(r"[sS](\d+)[. _-]*[eE](\d+)", file_name)
    
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        
        # 1. Remove the SXXEXX part and surrounding separators
        # Need a more flexible regex to remove the entire SXXEXX pattern
        normalized_name = re.sub(r"([. _-]+)?[sS]\d+[. _-]*[eE]\d+([. _-]+)?", " ", file_name).strip()
        
        # 2. Aggressively clean common garbage terms, quality, language, AND YEAR tags.
        common_terms = [
            "4K", "1080P", "720P", "HDRIP", "WEB-DL", "WEBRIP", "BLURAY", "HDTV", 
            "AMZN", "NETFLIX", "HULU", "DDP5.1", "X265", "x264", "10BIT", "MKV", 
            "MULTISUB", "AAC", "PSA", "DUB", "SUB", "MULTI", "REPACK", "COMPLETE", 
            "ENG", "SPA", "ITA", "FRE", "GER", "DUAL", "AUDIO", 
            r"(19|20)\d{2}", # Year tags
            "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08", "S09", "S10" 
        ]
        
        for term in common_terms:
            flags = re.IGNORECASE
            if r"(19|20)\d{2}" not in term:
                 term = r"\b" + term + r"\b"
            
            normalized_name = re.sub(term, " ", normalized_name, flags=flags).strip()


        # 3. Aggressively remove bracketed/parenthesized tags AND @channel tags
        normalized_name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", normalized_name).strip()
        
        # 4. ENHANCED CLEANING: Remove common channel/group tags that might remain as prefixes/suffixes (e.g., "CK The Flash")
        # Add other common tags you want to strip here
        channel_tags = ["CK", "UB", "TG", "MOVIE HUB", "FILM"] 
        for tag in channel_tags:
            # Match the tag at the start (^) or the end ($) of the string, followed/preceded by a space or separator
            # This handles patterns like "CK The Flash" or "The Flash - UB"
            tag_regex = rf"^\s*{re.escape(tag)}[\s._-]*|[\s._-]*{re.escape(tag)}\s*$"
            normalized_name = re.sub(tag_regex, " ", normalized_name, flags=re.IGNORECASE).strip()
            
        # 5. Final cleaning: collapse multiple separators, then title case it
        normalized_name = re.sub(r"[. _]+", " ", normalized_name).title().strip()
        
        # 6. LAST STEP: Truncate after the first strong separator (like a stray hyphen)
        if ' - ' in normalized_name:
             normalized_name = normalized_name.split(' - ')[0].strip()
        
        normalized_name = normalized_name.title().strip()
        
        return normalized_name, season, episode
    
    return file_name.title().strip(), 0, 0 


###################################################
## REAL-TIME FILE HANDLER (INDEXING LOGIC) ##
###################################################

media_filter = filters.document | filters.video | filters.audio

@app.on_message(filters.chat(CHANNELS) & media_filter)
async def media_handler(_, message):
    """
    Handles new media messages in the configured channels and indexes them if they are a series file.
    """
    
    file_name = ""
    if message.document:
        file_name = message.document.file_name or ""
    elif message.video:
        file_name = message.video.file_name or ""
    else: 
        file_name = getattr(message.audio, "file_name", "")

    normalized_name, season, episode = parse_series_info(file_name)
    
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
            logger.info(f"INDEXING: {normalized_name} S{season:02d}E{episode:02d} (Msg ID: {message.id})")
    else:
        logger.info(f"SKIP: Non-series file skipped ('{file_name}' from Msg ID: {message.id})")


###################################################
## PYROGRAM HANDLERS (User Interaction) ##
###################################################

async def send_startup_notification(client):
    logger.info("Bot restarted successfully.")


@app.on_message(filters.command("start") & filters.private)
async def start_command(_, message):
    """Handles the /start command."""
    welcome_text = (
        "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
        "Just send me the <b>name of a series</b> you are looking for, "
        "and I'll show you the available seasons and episodes from the index.\n\n"
        "<b>Example:</b> <code>Loki</code> or <code>Money Heist</code>"
    )
    await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

@app.on_message(filters.text & filters.private & ~filters.command("start"))
async def search_handler(_, message):
    """Handles user text search queries."""
    query = message.text.strip()
    
    unique_names = await search_series(query) 
    
    if unique_names:
        keyboard = []
        for name in sorted(unique_names):
            # This name is the cleaned 'normalized_name' from the database
            callback_data = f'series_select|{name}'
            # Display name is capped at 50 chars for button space
            button_text = name[:50] + '...' if len(name) > 50 else name 
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"🔎 Found {len(unique_names)} matching series. Please select one:"
        await message.reply_text(response, reply_markup=reply_markup)
    else:
        await message.reply_text(f"❌ No series files found matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)

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
    """Displays buttons for all unique seasons of the selected series in SXX format."""
    seasons = await get_unique_seasons_by_name(series_name) 
    
    if not seasons:
        try:
            await query.message.edit_text(text=f"❌ No seasons found for <b>{series_name}</b>.", parse_mode=enums.ParseMode.HTML) 
        except MessageNotModified:
            pass
        return

    tasks = [get_all_files_by_season(series_name, s) for s in seasons]
    all_season_files = await asyncio.gather(*tasks)
    total_files = sum(len(files_list) for files_list in all_season_files)
    
    keyboard = []
    row = []
    
    for season in seasons:
        callback_data = f'season_send|{series_name}|{season}'
        button_text = f"S{season:02d}" # SXX format
        
        row.append(InlineKeyboardButton(button_text, callback_data=callback_data))
        
        if len(row) >= 4: 
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)
    
    # Add Back and Close buttons as a final row
    final_row = []
    final_row.append(InlineKeyboardButton("⬅️ BACK", callback_data='go_back_to_search'))
    final_row.append(InlineKeyboardButton("❌ CLOSE", callback_data='close'))
    keyboard.append(final_row)


    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = (
        f"📺 <b>{series_name}</b>\n"
        f"<i>Total Files Indexed: {total_files} in {len(seasons)} seasons.</i>\n\n"
        "Select a Season:"
    )
    
    try:
        await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.HTML)
    except MessageNotModified:
        pass

@app.on_callback_query(filters.regex(r"go_back_to_search"))
async def go_back_to_search_handler(_, query):
     try:
        await query.message.edit_text(
             "Send a new series name to search for files."
         )
        await query.answer()
     except Exception:
        await query.answer("Cannot go back to search results.", show_alert=True)

@app.on_callback_query(filters.regex(r"close"))
async def close_menu_handler(_, query):
    try:
        await query.message.delete()
        await query.answer("Menu closed.")
    except Exception:
        await query.answer("Could not close the menu.")


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
                await app.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id
                )
                success_count += 1
                await asyncio.sleep(0.5) 
            except Exception as e:
                logger.error(f"Failed to copy message {message_id} from {channel_id}: {e}")

    final_message = f"✅ Sent <b>{success_count}</b> files for <b>{series_name}</b> Season {season:02d}."
    
    try:
        await status_message.edit_text(final_message, parse_mode=enums.ParseMode.HTML) 
    except Exception:
        await query.message.reply_text(final_message, parse_mode=enums.ParseMode.HTML) 


###################################################
## MAIN EXECUTION ##
###################################################

async def startup_task():
    await send_startup_notification(app)

if __name__ == "__main__":
    logger.info("Starting Pyrogram bot...")
    
    @app.on_raw_update()
    async def run_startup_task(client, update, users, chats):
        if not hasattr(client, '_startup_done'):
            client._startup_done = True
            await startup_task()

    try:
        app.run()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"An unexpected error occurred during bot execution: {e}")
