import re
import os
import asyncio
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified, ChannelInvalid, UsernameInvalid, UsernameNotModified, ChatAdminRequired
from pyrogram.errors.exceptions.flood_420 import FloodWait # Import FloodWait specifically
from pymongo import MongoClient, TEXT, DESCENDING 

# --- Configuration Placeholders (Your actual values) ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "8163888122:AAEuGzXZDxKlcehE2b5Yj4kpayBUo1VaZWs"
MONGO_URI = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# List of channel IDs (integers or strings) where files are posted and should be indexed.
CHANNELS = [-1001996538290] 

# --- Bulk Indexing Placeholders (REPLACE THESE) ---
# Ensure all ADMINS IDs are valid integers.
ADMINS = [5483128891]  # <-- REPLACE WITH YOUR ADMIN USER ID(s)
#
# FIX APPLIED: LOG_CHANNEL is a single integer, not a list.
#
LOG_CHANNEL = -1001996538290 # <-- REPLACE WITH YOUR LOG CHANNEL ID (NO BRACKETS [])
lock = asyncio.Lock()
class Temp:
    CURRENT = 1
    CANCEL = False
temp = Temp()


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
    """Inserts/updates a single file document into MongoDB. Returns True if saved/modified."""
    def sync_insert_one():
        try:
            result = files_collection.update_one(
                {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
                {"$set": file_doc},
                upsert=True
            )
            
            if result.upserted_id or result.modified_count > 0:
                logger.info(f"DB: INSERTED/UPDATED file. Msg ID: {file_doc['message_id']}")
                return True
            else:
                 logger.debug(f"DB: SKIPPED (Duplicate unchanged). Msg ID: {file_doc['message_id']}")
                 return False # False means duplicate/no change
        except Exception as e:
            logger.error(f"Failed to insert/update file doc: {e}")
            return False # False means error
            
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
        
    # Match SXXEXX patterns
    match = re.search(r"[sS](\d+)[. _-]*[eE](\d+)", file_name)
    
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        
        normalized_name = re.sub(r"([. _-]+)?[sS]\d+[. _-]*[eE]\d+([. _-]+)?", " ", file_name).strip()
        
        # 1. Clean common garbage terms, quality, language, AND YEAR tags.
        common_terms = [
            "4K", "1080P", "720P", "HDRIP", "WEB-DL", "WEBRIP", "BLURAY", "HDTV", 
            "AMZN", "NETFLIX", "HULU", "DDP5.1", "X265", "x264", "10BIT", "MKV", 
            "MULTISUB", "AAC", "PSA", "DUB", "SUB", "MULTI", "REPACK", "COMPLETE", 
            "ENG", "SPA", "ITA", "FRE", "GER", "DUAL", "AUDIO", 
            r"(19|20)\d{2}", 
            "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08", "S09", "S10" 
        ]
        
        for term in common_terms:
            flags = re.IGNORECASE
            if r"(19|20)\d{2}" not in term:
                 term = r"\b" + term + r"\b"
            
            normalized_name = re.sub(term, " ", normalized_name, flags=flags).strip()


        # 2. Aggressively remove bracketed/parenthesized tags AND @channel tags
        normalized_name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", normalized_name).strip()
        
        # 3. ENHANCED CLEANING: Remove common channel/group tags (e.g., "CK The Flash")
        channel_tags = ["CK", "UB", "TG", "MOVIE HUB", "FILM"] 
        for tag in channel_tags:
            tag_regex = rf"^\s*{re.escape(tag)}[\s._-]*|[\s._-]*{re.escape(tag)}\s*$"
            normalized_name = re.sub(tag_regex, " ", normalized_name, flags=re.IGNORECASE).strip()
            
        # 4. Final cleaning: collapse multiple separators, then title case it
        normalized_name = re.sub(r"[. _]+", " ", normalized_name).title().strip()
        
        # 5. LAST STEP: Truncate after the first strong separator (like a stray hyphen)
        if ' - ' in normalized_name:
             normalized_name = normalized_name.split(' - ')[0].strip()
        
        normalized_name = normalized_name.title().strip()
        
        return normalized_name, season, episode
    
    return file_name.title().strip(), 0, 0 


async def save_series_file_for_bulk_index(message):
    """
    Helper function to integrate series parsing/saving into the bulk indexer.
    Returns (True if new file saved, status_code)
    status_code: 1=new, 0=duplicate/skipped (non-series), 2=error.
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
        
        # This calls the MongoDB upsert function
        saved = await save_file_to_db(file_doc)
        
        if saved:
            return True, 1  # New file indexed
        else:
            # False from save_file_to_db means duplicate/no change
            return False, 0 
    else:
        # File is media, but not a series (no SXXEXX pattern)
        return False, 0 


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
## PYROGRAM HANDLERS (User Interaction) & BULK INDEXING ##
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
            callback_data = f'series_select|{name}'
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


# --- BULK INDEXING FUNCTIONS START HERE ---

@app.on_callback_query(filters.regex(r'^index'))
async def index_files(bot, query):
    if query.data.startswith('index_cancel'):
        temp.CANCEL = True
        return await query.answer("Cancelling Indexing")
    _, raju, chat, lst_msg_id, from_user = query.data.split("#")
    if raju == 'reject':
        await query.message.delete()
        await bot.send_message(int(from_user),
                               f'Your Submission for indexing {chat} has been decliened by our moderators.',
                               reply_to_message_id=int(lst_msg_id))
        return

    if lock.locked():
        return await query.answer('Wait until previous process complete.', show_alert=True)
    msg = query.message

    await query.answer('Processing...⏳', show_alert=True)
    if int(from_user) not in ADMINS:
        await bot.send_message(int(from_user),
                               f'Your Submission for indexing {chat} has been accepted by our moderators and will be added soon.',
                               reply_to_message_id=int(lst_msg_id))
    await msg.edit(
        "Starting Indexing",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
        )
    )
    try:
        # Ensure chat is treated as an integer if it's numeric
        chat = int(chat)
    except ValueError:
        pass # chat remains a string (username)
    except Exception:
        pass # chat remains as is

    await index_files_to_db(int(lst_msg_id), chat, msg, bot)


@app.on_message((filters.forwarded | (filters.regex("(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")) & filters.text ) & filters.private & filters.incoming)
async def send_for_index(bot, message):
    chat_id = None
    last_msg_id = None
    
    if message.text:
        regex = re.compile("(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
        match = regex.match(message.text)
        if not match:
            return await message.reply('Invalid link')
        chat_id_str = match.group(4)
        last_msg_id = int(match.group(5))
        
        # Consistent Pyrogram channel ID conversion
        if chat_id_str.isnumeric():
            # Convert numeric group ID (e.g., 12345) to Pyrogram chat ID format (-10012345)
            chat_id = int(("-100" + chat_id_str))
        else:
            chat_id = chat_id_str # Keeps username as string
            
    elif message.forward_from_chat and message.forward_from_chat.type == enums.ChatType.CHANNEL:
        last_msg_id = message.forward_from_message_id
        chat_id = message.forward_from_chat.username or message.forward_from_chat.id
    else:
        return # Not a link or channel forward

    # --- Access Check (Source of CHANNEL_INVALID) ---
    try:
        # This line attempts to resolve the chat_id. It will fail if the bot 
        # is not an admin/member of a private chat, or if the ID is invalid.
        await bot.get_chat(chat_id)
    except ChannelInvalid:
        # Specific error for invalid/inaccessible channels
        return await message.reply('This may be a private channel / group. Make me an admin over there to index the files.')
    except (UsernameInvalid, UsernameNotModified):
        return await message.reply('Invalid Link specified.')
    except Exception as e:
        logger.exception(e)
        return await message.reply(f'Errors - {e}')
        
    # Second check for message existence/admin status
    try:
        k = await bot.get_messages(chat_id, last_msg_id)
    except Exception:
        # The first check often covers this, but this handles the message ID range check.
        return await message.reply('Make Sure That Iam An Admin In The Channel, if channel is private, and the message ID exists.')
    if k.empty:
        return await message.reply('This may be group and iam not a admin of the group or the message ID is too high.')

    if message.from_user.id in ADMINS:
        buttons = [
            [
                InlineKeyboardButton('Yes',
                                    callback_data=f'index#accept#{chat_id}#{last_msg_id}#{message.from_user.id}')
            ],
            [
                InlineKeyboardButton('close', callback_data='close_data'),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        return await message.reply(
            f'Do you Want To Index This Channel/ Group ?\n\nChat ID/ Username: <code>{chat_id}</code>\nLast Message ID: <code>{last_msg_id}</code>',
            reply_markup=reply_markup)

    if type(chat_id) is int:
        try:
            link = (await bot.create_chat_invite_link(chat_id)).invite_link
        except ChatAdminRequired:
            return await message.reply('Make sure iam an admin in the chat and have permission to invite users.')
        except Exception:
            link = "N/A" # Default if invite link creation fails for private chat
    else:
        link = f"@{message.forward_from_chat.username}"
    buttons = [
        [
            InlineKeyboardButton('Accept Index',
                                    callback_data=f'index#accept#{chat_id}#{last_msg_id}#{message.from_user.id}')
        ],
        [
            InlineKeyboardButton('Reject Index',
                                    callback_data=f'index#reject#{chat_id}#{message.id}#{message.from_user.id}'),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await bot.send_message(LOG_CHANNEL,
                           f'#IndexRequest\n\nBy : {message.from_user.mention} (<code>{message.from_user.id}</code>)\nChat ID/ Username - <code> {chat_id}</code>\nLast Message ID - <code>{last_msg_id}</code>\nInviteLink - {link}',
                           reply_markup=reply_markup)
    await message.reply('ThankYou For the Contribution, Wait For My Moderators to verify the files.')


@app.on_message(filters.command('setskip') & filters.user(ADMINS))
async def set_skip_number(bot, message):
    if ' ' in message.text:
        _, skip = message.text.split(" ")
        try:
            skip = int(skip)
        except:
            return await message.reply("Skip number should be an integer.")
        await message.reply(f"Successfully set SKIP number as {skip}")
        temp.CURRENT = int(skip)
    else:
        await message.reply("Give me a skip number")


async def index_files_to_db(lst_msg_id, chat, msg, bot):
    total_files = 0
    duplicate = 0
    errors = 0
    deleted = 0
    no_media = 0
    unsupported = 0
    
    # Initialize with the maximum message ID to start with
    current_offset_id = lst_msg_id
    
    async with lock:
        try:
            temp.CANCEL = False
            current_processed_count = 0
            
            while True:
                if temp.CANCEL:
                    await msg.edit(f"Successfully Cancelled!!\n\nSaved <code>{total_files}</code> files to dataBase!\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>")
                    break

                # Determine the message IDs to fetch in this batch (100 messages)
                start_id = max(1, current_offset_id - 100)
                
                # Fetch messages using a range of message IDs (Bot-safe history retrieval)
                messages = await bot.get_messages(
                    chat_id=chat, 
                    message_ids=list(range(start_id, current_offset_id)),
                )
                
                if not messages:
                    break # Reached the beginning of the chat or no more messages in the range

                # Sort messages in reverse order (highest ID first) since we iterate backward
                messages.sort(key=lambda m: m.id, reverse=True)

                for message in messages:
                    if temp.CANCEL:
                        break

                    current_processed_count += 1
                    
                    # Update status every 20 processed messages
                    if current_processed_count % 20 == 0:
                        can = [[InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
                        reply = InlineKeyboardMarkup(can)
                        await msg.edit_text(
                            text=f"Total messages fetched: <code>{current_processed_count}</code>\nTotal messages saved: <code>{total_files}</code>\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>",
                            reply_markup=reply)

                    # --- Skipping/Resuming Logic ---
                    if temp.CURRENT > 1 and message.id > temp.CURRENT:
                        continue

                    temp.CURRENT = 1
                    
                    # --- START: Series Indexing Logic ---
                    if message.empty:
                        deleted += 1
                        continue
                    elif not message.media:
                        no_media += 1
                        continue
                    elif message.media not in [enums.MessageMediaType.VIDEO, enums.MessageMediaType.AUDIO, enums.MessageMediaType.DOCUMENT]:
                        unsupported += 1
                        continue

                    is_saved, status_code = await save_series_file_for_bulk_index(message)
                    
                    if is_saved:
                        total_files += 1
                    elif status_code == 0:
                        duplicate += 1
                    elif status_code == 2:
                        errors += 1
                    # --- END: Series Indexing Logic ---
                    
                # Break if canceled during message processing
                if temp.CANCEL:
                    break
                    
                # Set the offset for the next batch to the lowest ID in the current batch
                current_offset_id = messages[-1].id if messages else 0
                
                # --- FIX: Add a small delay to avoid FloodWait ---
                await asyncio.sleep(0.5) # Wait half a second after processing a batch of up to 100 messages
                # --- END FIX ---
                
                # Check if we have processed all messages (message ID 1 is the first)
                if current_offset_id <= 1:
                    break

        except Exception as e:
            # --- FIX: Handle FloodWait explicitly ---
            if isinstance(e, FloodWait):
                logger.error(f"Hit FloodWait: Waiting for {e.value} seconds.")
                # Report to the user and wait the required time + 1 second buffer
                await msg.edit(f'⚠️ FloodWait hit! Pausing indexing for **{e.value}** seconds to comply with Telegram API limits.')
                await asyncio.sleep(e.value + 1)
                continue # Continue the outer while loop to fetch the next batch
            
            # Handle other errors
            logger.exception(e)
            await msg.edit(f'Error: {e}')
        else:
            await msg.edit(f'✅ Succesfully saved <code>{total_files}</code> to dataBase!\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>')


###################################################
## MAIN EXECUTION ##
###################################################

if __name__ == "__main__":
    logger.info("Starting Pyrogram bot...")
    
    @app.on_raw_update()
    async def run_startup_task(client, update, users, chats):
        if not hasattr(client, '_startup_done'):
            client._startup_done = True
            await send_startup_notification(app)

    try:
        app.run()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"An unexpected error occurred during bot execution: {e}")
