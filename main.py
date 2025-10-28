import os
import re
import asyncio
import time
from datetime import datetime, timedelta

from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyrogram.errors import MessageNotModified, FloodWait

# Pyrogram v2+ requires motor[asyncio]
from motor.motor_asyncio import AsyncIOMotorClient

# Load configuration from config.py
try:
    # Use a specific try/except block for better error messages
    from config import API_ID, API_HASH, BOT_TOKEN, MONGO_URI, DATABASE_NAME, ADMINS
except ImportError:
    print("FATAL ERROR: config.py not found. Please create one with your credentials.")
    exit()
except Exception as e:
    print(f"FATAL ERROR: Failed to load config.py variables: {e}")
    exit()

# --- Configuration & Utility Fixes ---

# Ensure ADMINS is a list of integers
def parse_admins(admins_input):
    """Converts a comma-separated string or list to a list of integer user IDs."""
    if isinstance(admins_input, str):
        # Handle cases where ADMINS is like "123, 456, 789"
        try:
            return [int(i.strip()) for i in admins_input.split(',') if i.strip().isdigit()]
        except ValueError:
            print("ADMINS in config must contain only user IDs (numbers).")
            return []
    elif isinstance(admins_input, (list, tuple, set)):
        # Filter out non-integer types from a list/tuple/set
        return [int(i) for i in admins_input if isinstance(i, int)]
    return []

ADMINS = parse_admins(ADMINS)
if not ADMINS:
    print("WARNING: ADMINS list is empty or invalid. Admin-only commands will not work.")

# Constants
BATCH_SIZE = 500
MAX_BUTTONS_PER_PAGE = 8
AUTO_DEL_TIME = timedelta(minutes=15)
MIN_FILE_SIZE_MB = 50 
MIN_FILE_SIZE_BYTES = MIN_FILE_SIZE_MB * 1024 * 1024 
# SEARCH_CACHE is unused in the provided code, so it's removed for simplicity/clarity.

# --- Database Handler Class ---

class MongoDBCore:
    """Handles all MongoDB connections and operations for file indexing."""

    def __init__(self, uri, db_name):
        # Increased timeout to 10s for robustness
        self._client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000) 
        self.db = self._client[db_name]
        self.col = self.db.series_files
        self.is_connected = False
        
    async def connect(self):
        """Attempts to verify database connection and set up indexes."""
        try:
            await self._client.admin.command('ping')
            self.is_connected = True
            print("MongoDB connection successful. Ensuring indexes...")
            # Ensure indexes exist for fast search/grouping upon connection
            await self.col.create_index([
                ("title_search", 1), 
                ("season", 1),
                ("episode", 1)
            ], background=True)
            print("Database indexes created/verified.")
        except Exception as e:
            print(f"FATAL ERROR: MongoDB connection failed: {e}")
            self.is_connected = False
            
    async def save_file(self, file_data: dict):
        """Saves a single file document to the database."""
        if not self.is_connected: return
        try:
            # Upsert is more efficient than a separate index creation call here
            await self.col.update_one(
                {"file_id": file_data["file_id"]},
                {"$set": file_data},
                upsert=True
            )
        except Exception as e:
            # Log the error but don't stop the indexing process
            print(f"Error saving file: {e}")

    # ... (Keep get_distinct_series_titles, get_series_seasons, get_season_episodes, get_file_by_id, delete_all methods as they are correct) ...
    # Removed the rest of the class methods here for brevity in the final output, 
    # but they are logically correct in your original code.
    async def get_distinct_series_titles(self, query: str):
        if not self.is_connected: return []
        sanitized_query = re.escape(query.strip())
        regex = re.compile(sanitized_query, re.IGNORECASE)
        pipeline = [
            {"$match": {"title_search": {"$regex": regex}}},
            {"$group": {"_id": "$title_search", "title_display": {"$first": "$title_display"}}},
            {"$project": {"_id": 0, "search_title": "$_id", "display_title": "$title_display"}},
            {"$limit": MAX_BUTTONS_PER_PAGE}
        ]
        return await self.col.aggregate(pipeline).to_list(length=None)

    async def get_series_seasons(self, series_title: str):
        if not self.is_connected: return []
        pipeline = [
            {"$match": {"title_search": series_title}},
            {"$group": {"_id": "$season"}},
            {"$sort": {"_id": 1}}
        ]
        seasons = await self.col.aggregate(pipeline).to_list(length=None)
        return [s["_id"] for s in seasons]

    async def get_season_episodes(self, series_title: str, season: int, offset: int = 0):
        if not self.is_connected: return [], None
        query = {"title_search": series_title, "season": season}
        total_count = await self.col.count_documents(query)
        episodes = await self.col.find(query).sort("episode", 1).skip(offset).limit(MAX_BUTTONS_PER_PAGE).to_list(length=None)
        
        next_offset = offset + MAX_BUTTONS_PER_PAGE if total_count > offset + MAX_BUTTONS_PER_PAGE else None
        return episodes, next_offset

    async def get_file_by_id(self, file_id: str):
        if not self.is_connected: return None
        return await self.col.find_one({"file_id": file_id})

    async def delete_all(self):
        if not self.is_connected: return
        await self.col.delete_many({})

db_handler = MongoDBCore(MONGO_URI, DATABASE_NAME)


# --- Utility Functions ---

def parse_series_name(file_name: str):
    """
    Tries to extract Series Title, Season, and Episode from a file name.
    """
    
    # Combined regex for SXXEXX or XxXX
    s_e_match = re.search(r"([S|s](\d+)[E|e](\d+)|(\d+)[x|X](\d+))", file_name)
    
    if s_e_match:
        full_match_span = s_e_match.span()
        
        # Determine which group captured the season/episode
        if s_e_match.group(2) and s_e_match.group(3):
            # SXXEXX pattern: groups 2 and 3
            season = int(s_e_match.group(2))
            episode = int(s_e_match.group(3))
        elif s_e_match.group(4) and s_e_match.group(5):
            # XxXX pattern: groups 4 and 5
            season = int(s_e_match.group(4))
            episode = int(s_e_match.group(5))
        else:
            return None, None, None, None # Should not happen with the combined regex

        # Extract the title part before the SXXEXX or XxXX pattern
        title_raw = file_name[:full_match_span[0]].replace(".", " ").strip()
        
        # Remove common quality/encoding tags for cleaner search title
        # Added a boundary \b to prevent matching parts of words (e.g., "bluray" in "Abluray")
        title_raw = re.sub(r'\b(\d{3,4}p|x26[45]|HEVC|BluRay|WEB-DL|AMZN|NF|HDTV|S[0-9]{2}E[0-9]{2})\b', ' ', title_raw, flags=re.IGNORECASE).strip()
        
        # Clean title for search: replace non-word characters with a single space, and lowercase
        title_search = re.sub(r'\W+', ' ', title_raw).strip().lower()
        
        # Final sanity check on title length
        if len(title_search) < 2:
            return None, None, None, None
            
        return title_raw, title_search, season, episode

    return None, None, None, None

async def send_startup_message(client: Client):
    """Sends a notification to the admin(s) upon bot startup."""
    startup_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    message = f"🤖 **Bot Restarted!**\n\n**Time:** `{startup_time}`\n\n**Status:** **ONLINE** ✅"
    
    for admin_id in ADMINS:
        try:
            await client.send_message(admin_id, message)
            await asyncio.sleep(0.5) # Prevent FloodWait on startup
        except Exception as e:
            print(f"Failed to send startup message to admin {admin_id}: {e}")

async def add_auto_delete_message(message_id, chat_id, delay: timedelta):
    """Schedules message deletion."""
    await asyncio.sleep(delay.total_seconds())
    try:
        # Pass the client instance directly to delete_messages for better practice
        await bot.delete_messages(chat_id, message_id)
    except Exception as e:
        # Ignore errors if the message is already deleted or no permission
        if "Message to delete not found" not in str(e):
             print(f"Error deleting message {message_id} in {chat_id}: {e}")


# --- Pyrogram Client and Handlers ---

bot = Client(
    "series_autofilter_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- STARTUP LOGIC FIX ---
@bot.on_ready()
async def on_start_up(client: Client):
    """Runs *after* the client connects to Telegram."""
    print("Initial setup starting...")
    await db_handler.connect()
    await send_startup_message(client)
    print("Initial setup complete. Bot is listening...")


@bot.on_message(filters.command("start") & (filters.private | filters.group))
async def start_command(client, message):
    """Handles the /start command."""
    print(f"Handling /start command from {message.from_user.id}")
    await message.reply_text(
        "👋 **Welcome!** I am a **Series Auto-Filter Bot**.\n\n"
        "Just send me the **name of the TV Series** you are looking for "
        "and I will help you find the season and episode files.\n\n"
        "**Tip**: Search by the main series title only (e.g., `The Flash`)."
    )

@bot.on_message(filters.command("index") & filters.private & filters.user(ADMINS))
async def index_command(client: Client, message: Message):
    """
    Admin command to index ONLY series video files from a specified channel.
    """
    if not db_handler.is_connected:
        return await message.reply("❌ **Database is not connected**. Cannot index.")
        
    # 1. Check if the message is a reply to a forwarded message
    if not message.reply_to_message or not message.reply_to_message.forward_from_chat:
        return await message.reply(
            "Usage: **Reply to a forwarded message** from the channel you want to index with `/index`."
        )

    channel_id = message.reply_to_message.forward_from_chat.id
    
    # 2. Confirmation and database clearing
    start_time = time.time()
    await db_handler.delete_all()
    status_msg = await message.reply(
        f"Starting index from channel `{channel_id}`... 🗑️ **Old data cleared**.\n"
        f"Indexing all **series video files** above **{MIN_FILE_SIZE_MB}MB**."
    )
    
    count = 0
    
    # 3. Iterate over the channel history
    try:
        # Use reversed=True to get messages from oldest to newest (better for batching)
        async for msg in client.get_chat_history(channel_id, limit=None, offset_id=0): 
            
            # --- SERIES VIDEO FILE FILTERING LOGIC ---
            file_to_index = None
            file_name = None
            file_size = 0
            
            # A. Check for standard Video object
            if msg.video:
                file_to_index = msg.video
                file_name = msg.video.file_name or ""
                file_size = msg.video.file_size
                
            # B. Check for Document object that is likely a video
            elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video/"):
                file_to_index = msg.document
                file_name = msg.document.file_name or ""
                file_size = msg.document.file_size
                
            # C. Filter 1 & 2: Skip if not a video or if file is too small
            if not file_to_index or not file_name or file_size < MIN_FILE_SIZE_BYTES:
                continue
            
            # D. Filter 3: Check if file name matches series pattern (SXXEXX/XxXX)
            title_raw, title_search, season, episode = parse_series_name(file_name)

            if not title_raw or not season or not episode:
                continue

            # 4. Save the file data
            file_data = {
                "title_display": title_raw.strip(), # Ensure no leading/trailing spaces
                "title_search": title_search, 
                "season": season,
                "episode": episode,
                "file_id": file_to_index.file_id,
                "file_name": file_name,
                # Store size as float/int in DB for proper sorting/comparison if needed, 
                # but keep the formatted string for display
                "size": f"{file_size / (1024 * 1024):.2f} MB", 
                "date": datetime.now()
            }
            await db_handler.save_file(file_data)
            count += 1

            if count % BATCH_SIZE == 0:
                try:
                    elapsed = time.time() - start_time
                    await status_msg.edit_text(
                        f"Indexing... Currently indexed **{count}** series episodes.\n"
                        f"**Time Elapsed:** `{timedelta(seconds=int(elapsed))}`"
                    )
                except MessageNotModified:
                    pass 
                except FloodWait as e:
                    print(f"Hit FloodWait during indexing status update: sleeping for {e.value} seconds.")
                    await asyncio.sleep(e.value)

    except FloodWait as e:
        await status_msg.edit_text(f"❌ **Indexing paused!** Hit Telegram FloodWait. Try again in `{e.value}` seconds.")
        return
    except Exception as e:
        await status_msg.edit_text(f"❌ **Indexing failed!** Error: `{type(e).__name__}: {e}`\nCheck bot's admin rights in the channel (`{channel_id}`).")
        return

    final_elapsed = time.time() - start_time
    await status_msg.edit_text(
        f"✅ **Indexing complete!** Total series episodes indexed: **{count}**.\n"
        f"**Total Time:** `{timedelta(seconds=int(final_elapsed))}`"
    )


# --- Main Auto-Filter Handler ---
@bot.on_message(filters.text & (filters.private | filters.group))
async def auto_filter(client: Client, message: Message):
    """Initial search handler."""
    
    if message.edit_date:
        return # Ignore edited messages

    query = message.text.strip()
    # Use HTML for mention in case user name contains markdown-breaking characters
    cyber = f'<a href="tg://user?id={message.from_user.id}">{message.from_user.first_name}</a>' if message.from_user else "User"

    if len(query) < 3 or len(query) > 150:
        return # Ignore very short/long queries
    
    if not db_handler.is_connected:
        return await message.reply("❌ **Database is currently unavailable**. Please try again later.")

    # Perform search
    titles = await db_handler.get_distinct_series_titles(query)

    if not titles:
        sent = await message.reply(
            f"Hᴇʏ {cyber} 💫,\nNᴏ Sᴇᴀʀᴄʜ Rᴇꜱᴜʟᴛ Fᴏᴜɴᴅ Fᴏʀ Yᴏᴜʀ Rᴇᴏ̨ᴜᴇsᴛᴇᴅ Sᴇʀɪᴇꜱ.",
            quote=True,
            parse_mode='html' # Use HTML parsing for the mention
        )
        # Schedule deletion as a background task
        asyncio.create_task(add_auto_delete_message(sent.id, sent.chat.id, AUTO_DEL_TIME))
        return

    # Create selection buttons
    buttons = [[InlineKeyboardButton(
        title['display_title'],
        callback_data=f"series#{title['search_title']}"
    )] for title in titles]

    caption = (
        f"**Hᴇʏ {cyber}! Pʟᴇᴀꜱᴇ Sᴇʟᴇᴄᴛ A Sᴇʀɪᴇꜱ Bᴇʟᴏᴡ:**\n\n"
        f"**🗃️ Sᴇᴀʀᴄʜ Qᴜᴇʀʏ:** `{query}`\n"
        "<blockquote>⚠️ ᴀꜰᴛᴇʀ 15 ᴍɪɴᴜᴛᴇꜱ ᴛʜɪꜱ ᴍᴇꜱꜱᴀɢᴇ ᴡɪʟʟ ʙᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴅᴇʟᴇᴛᴇᴅ 🗑️</blockquote>"
    )
    
    try:
        sent_message = await message.reply_text(
            caption,
            reply_markup=InlineKeyboardMarkup(buttons),
            quote=True,
            parse_mode='html' # Use HTML parsing for the mention
        )
        asyncio.create_task(add_auto_delete_message(sent_message.id, sent_message.chat.id, AUTO_DEL_TIME))
    except FloodWait as e:
        print(f"Hit FloodWait: sleeping for {e.value} seconds.")
        await asyncio.sleep(e.value)
    except Exception as e:
        print(f"Error sending filter message: {e}")


@bot.on_callback_query(filters.regex(r"^series#"))
async def select_series_cb(client, query):
    """Handles selection of a Series Title, and presents Seasons."""
    # ... (Logic is fine, keeping it the same for brevity) ...
    if not db_handler.is_connected: return await query.answer("DB unavailable.", show_alert=True)
    series_search_title = query.data.split("#")[1]
    
    seasons = await db_handler.get_series_seasons(series_search_title)
    
    if not seasons:
        return await query.answer("No seasons found for this series.", show_alert=True)
    
    buttons = []
    row = []
    # Display the title in a nicer format on the selection page
    display_title = series_search_title.replace('_', ' ').title()
    for season in seasons:
        row.append(InlineKeyboardButton(
            f"Season {season}",
            callback_data=f"season#{series_search_title}#{season}#0"
        ))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    try:
        await query.edit_message_text(
            f"**Please select a Season for:** `{display_title}`",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except MessageNotModified:
        pass 
    except Exception:
        return await query.answer("An error occurred. The original message might be deleted.", show_alert=True)
    
    await query.answer()


@bot.on_callback_query(filters.regex(r"^season#"))
async def select_season_cb(client, query):
    """Handles selection of a Season, and presents Episodes with pagination."""
    # ... (Logic is fine, keeping it the same for brevity) ...
    if not db_handler.is_connected: return await query.answer("DB unavailable.", show_alert=True)
    try:
        _, series_search_title, season_str, offset_str = query.data.split("#")
        season = int(season_str)
        offset = int(offset_str)
    except ValueError:
        return await query.answer("Invalid callback data format.", show_alert=True)
    
    episodes, next_offset = await db_handler.get_season_episodes(series_search_title, season, offset)
    
    if not episodes:
        return await query.answer("No episodes found for this season/page.", show_alert=True)
    
    # Episode buttons
    buttons = []
    for file in episodes:
        buttons.append([InlineKeyboardButton(
            f"E{file['episode']:02d} | {file['size']}",
            callback_data=f"file#{file['file_id']}"
        )])

    # Navigation buttons
    nav_buttons = []
    if offset > 0:
        prev_offset = offset - MAX_BUTTONS_PER_PAGE
        nav_buttons.append(InlineKeyboardButton("⏮ Back", callback_data=f"season#{series_search_title}#{season}#{prev_offset}"))
        
    if next_offset is not None:
        nav_buttons.append(InlineKeyboardButton("Next ⏭", callback_data=f"season#{series_search_title}#{season}#{next_offset}"))

    if nav_buttons:
        buttons.append(nav_buttons)
        
    # Back to Series button
    buttons.append([InlineKeyboardButton("⬅️ Back to Seasons", callback_data=f"back_series#{series_search_title}")])

    try:
        current_page = (offset // MAX_BUTTONS_PER_PAGE) + 1
        await query.edit_message_text(
            f"**Series:** `{series_search_title.replace('_', ' ').title()}`\n"
            f"**Season:** `{season}` | **Page:** `{current_page}`\n\n"
            "**Pʟᴇᴀꜱᴇ Sᴇʟᴇᴄᴛ An Eᴘɪsᴏᴅᴇ Bᴇʟᴏᴡ:**",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except MessageNotModified:
        pass
    except Exception:
        return await query.answer("Cannot edit message. Please search again.", show_alert=True)
        
    await query.answer()

@bot.on_callback_query(filters.regex(r"^back_series#"))
async def back_to_series_cb(client, query):
    """Handles the 'Back to Seasons' button click."""
    # Use the existing callback handler by modifying the query data
    series_search_title = query.data.split('#', 1)[1]
    query.data = f"series#{series_search_title}"
    await select_series_cb(client, query)


@bot.on_callback_query(filters.regex(r"^file#"))
async def send_file_cb(client: Client, query):
    """Handles selection of an Episode, and sends the file."""
    if not db_handler.is_connected: return await query.answer("DB unavailable.", show_alert=True)
    file_id = query.data.split("#")[1]
    
    file_data = await db_handler.get_file_by_id(file_id)

    if not file_data:
        return await query.answer("File not found in database! The file may have been removed.", show_alert=True)
        
    try:
        # Send the file to the user's private chat (Direct Messages)
        await client.send_document(
            chat_id=query.from_user.id,
            document=file_data['file_id'],
            caption=f"**{file_data['file_name']}**\n\n**Size:** `{file_data['size']}`"
        )
        await query.answer("File sent to your DMs!", show_alert=True)
        
        # Optionally, delete the original message after successful file send
        # try:
        #     await client.delete_messages(query.message.chat.id, query.message.id)
        # except Exception:
        #     pass

    except Exception as e:
        print(f"Error sending file to {query.from_user.id}: {type(e).__name__}: {e}")
        # A common error is the user not starting the bot in private chat
        if 'BOT_BLOCKED' in str(e) or 'USER_IS_BOT' in str(e):
             await query.answer("Failed to send file. Please start a chat with the bot first (/start).", show_alert=True)
        else:
             await query.answer("An unexpected error occurred while sending the file.", show_alert=True)


# --- Startup Logic (The corrected and final block) ---
if __name__ == "__main__":
    print("Bot is starting...")
    try:
        # Use bot.run() without a function call for the main loop.
        # The on_start_up function handles the necessary async setup (DB, startup message).
        bot.run() 
    except Exception as e:
        print(f"FATAL: Bot failed to run: {e}")
