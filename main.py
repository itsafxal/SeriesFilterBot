import os
import re
import asyncio
from datetime import datetime, timedelta

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyrogram.errors import MessageNotModified
from motor.motor_asyncio import AsyncIOMotorClient
from config import API_ID, API_HASH, BOT_TOKEN, MONGO_URI, DATABASE_NAME, ADMINS

# Constants
BATCH_SIZE = 500
MAX_BUTTONS_PER_PAGE = 8
AUTO_DEL_TIME = timedelta(minutes=15)
SEARCH_CACHE = {} # Simple in-memory cache for search queries

# --- Database Handler Class ---

class MongoDBCore:
    """Handles all MongoDB connections and operations for file indexing."""

    def __init__(self, uri, db_name):
        self._client = AsyncIOMotorClient(uri)
        self.db = self._client[db_name]
        self.col = self.db.series_files # Collection for series files

    async def save_file(self, file_data: dict):
        """Saves a single file document to the database."""
        # Ensure 'title' and 'season' are indexed for fast searching
        await self.col.create_index([
            ("title", 1),
            ("season", 1),
            ("episode", 1)
        ], background=True)
        try:
            await self.col.insert_one(file_data)
        except Exception as e:
            print(f"Error saving file: {e}")

    async def get_distinct_series_titles(self, query: str):
        """
        Finds distinct series titles that match the search query.
        Uses regex for flexible searching.
        """
        regex = re.compile(re.escape(query), re.IGNORECASE)
        pipeline = [
            {"$match": {"title_search": {"$regex": regex}}},
            {"$group": {"_id": "$title_search", "title": {"$first": "$title_display"}}},
            {"$limit": MAX_BUTTONS_PER_PAGE} # Limit results for initial search
        ]
        titles = await self.col.aggregate(pipeline).to_list(length=None)
        return [{"search_title": t["_id"], "display_title": t["title"]} for t in titles]

    async def get_series_seasons(self, series_title: str):
        """Returns distinct season numbers for a given series title."""
        pipeline = [
            {"$match": {"title_search": series_title}},
            {"$group": {"_id": "$season"}},
            {"$sort": {"_id": 1}} # Sort seasons numerically
        ]
        seasons = await self.col.aggregate(pipeline).to_list(length=None)
        return [s["_id"] for s in seasons]

    async def get_season_episodes(self, series_title: str, season: int, offset: int = 0):
        """Returns episodes for a specific series and season."""
        query = {
            "title_search": series_title,
            "season": season
        }
        
        # Count total episodes for pagination logic
        total_count = await self.col.count_documents(query)

        episodes = await self.col.find(query).sort("episode", 1).skip(offset).limit(MAX_BUTTONS_PER_PAGE).to_list(length=None)
        
        # Calculate next offset
        next_offset = offset + MAX_BUTTONS_PER_PAGE if total_count > offset + MAX_BUTTONS_PER_PAGE else None

        return episodes, next_offset

    async def get_file_by_id(self, file_id: str):
        """Retrieves file details by its file_id."""
        return await self.col.find_one({"file_id": file_id})

    async def delete_all(self):
        """Deletes all documents in the collection (for re-indexing)."""
        await self.col.delete_many({})

db_handler = MongoDBCore(MONGO_URI, DATABASE_NAME)


# --- Utility Functions ---

def parse_series_name(file_name: str):
    """
    Tries to extract Series Title, Season, and Episode from a file name.
    Example patterns: S01E05, s01e05, Season 1 Episode 5, 1x05
    """
    
    # 1. SXXEXX pattern (most common)
    s_e_match = re.search(r"[S|s](\d+)[E|e](\d+)", file_name)
    if s_e_match:
        season = int(s_e_match.group(1))
        episode = int(s_e_match.group(2))
        # Title is everything before SXXEXX, cleaned up
        title_raw = file_name[:s_e_match.start()].replace(".", " ").strip()
        title_search = re.sub(r'\W+', ' ', title_raw).strip().lower()
        return title_raw, title_search, season, episode

    # 2. XxXX pattern (less common, e.g., 1x05)
    x_match = re.search(r"(\d+)[x|X](\d+)", file_name)
    if x_match:
        season = int(x_match.group(1))
        episode = int(x_match.group(2))
        title_raw = file_name[:x_match.start()].replace(".", " ").strip()
        title_search = re.sub(r'\W+', ' ', title_raw).strip().lower()
        return title_raw, title_search, season, episode

    # If no pattern matches, it's not indexed as a series
    return None, None, None, None

async def add_auto_delete_message(message_id, chat_id, delay: timedelta):
    """Simulates auto-delete by scheduling a task."""
    await asyncio.sleep(delay.total_seconds())
    try:
        await bot.delete_messages(chat_id, message_id)
    except Exception as e:
        print(f"Failed to delete message: {e}")

# --- Pyrogram Client and Handlers ---

bot = Client(
    "series_autofilter_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@bot.on_message(filters.command("start"))
async def start_command(client, message):
    """Handles the /start command."""
    await message.reply_text(
        "👋 **Welcome!** I am a Series Auto-Filter Bot.\n\n"
        "Just send me the name of the TV Series you are looking for, "
        "and I will help you find the season and episode files.\n\n"
        "**Tip**: Search by the main series title only (e.g., `The Flash`)."
    )

@bot.on_message(filters.command("index") & filters.private & filters.user(ADMINS))
async def index_command(client: Client, message: Message):
    """Admin command to index files from a specified channel."""
    if len(message.command) < 2 or not message.command[1].isdigit():
        return await message.reply("Usage: `/index <channel_id>` (e.g., `/index -1001234567890`)")

    channel_id = int(message.command[1])
    count = 0
    await db_handler.delete_all()
    status_msg = await message.reply("Starting index... 🗑️ Old data cleared.")

    async for msg in client.search_messages(channel_id, filter="document"):
        # Skip messages without media or file name
        if not msg.document or not msg.document.file_name:
            continue
            
        # Parse the file name
        title_raw, title_search, season, episode = parse_series_name(msg.document.file_name)

        if not title_raw or not season or not episode:
            # Skip non-series files or files that don't match pattern
            continue

        file_data = {
            "title_display": title_raw,
            "title_search": title_search, # Lowercase and sanitized for search
            "season": season,
            "episode": episode,
            "file_id": msg.document.file_id,
            "file_name": msg.document.file_name,
            "size": f"{msg.document.file_size / (1024 * 1024):.2f} MB" if msg.document.file_size else "N/A",
            "date": datetime.now()
        }
        await db_handler.save_file(file_data)
        count += 1

        if count % BATCH_SIZE == 0:
            await status_msg.edit_text(f"Indexing... Currently indexed **{count}** files.")

    await status_msg.edit_text(f"✅ **Indexing complete!** Total series episodes indexed: **{count}**.")


@bot.on_message(filters.text & filters.private & filters.incoming & filters.reply | filters.text & filters.group & filters.incoming)
async def auto_filter(client: Client, message: Message):
    """
    Initial search handler. Finds series titles matching the query.
    This replaces the user's initial auto_filter function.
    """
    query = message.text.strip()
    cyber = message.from_user.mention

    if len(query) < 3 or len(query) > 150:
        return # Ignore short or overly long queries
    
    # 1. Search for matching series titles
    titles = await db_handler.get_distinct_series_titles(query)

    if not titles:
        sent = await message.reply(
            f"Hᴇʏ {cyber} 💫,\nNᴏ Sᴇᴀʀᴄʜ Rᴇꜱᴜʟᴛ Fᴏᴜɴᴅ Fᴏʀ Yᴏᴜʀ Rᴇᴏ̨ᴜᴇsᴛᴇᴅ Sᴇʀɪᴇꜱ.\n\n"
            "➪ Sᴇɴᴅ Mᴇ A Cᴏʀʀᴇᴄᴛ Sᴘᴇʟʟɪɴɢ Oʀ Pʟᴇᴀꜱᴇ Cʜᴇᴄᴋ Fᴏʀ Thᴇ Sᴇʀɪᴇꜱ Tɪᴛʟᴇ.✨",
            quote=True
        )
        await add_auto_delete_message(sent.id, sent.chat.id, AUTO_DEL_TIME)
        return

    # 2. Generate buttons for series selection
    buttons = [[InlineKeyboardButton(
        title['display_title'],
        callback_data=f"series#{title['search_title']}"
    )] for title in titles]

    # Save search query for pagination/state management
    search_key = f"{message.from_user.id}_{message.id}"
    SEARCH_CACHE[search_key] = query

    caption = (
        f"**Hᴇʏ {cyber}! Pʟᴇᴀꜱᴇ Sᴇʟᴇᴄᴛ A Sᴇʀɪᴇꜱ Bᴇʟᴏᴡ:**\n\n"
        f"**🗃️ Sᴇᴀʀᴄʜ Qᴜᴇʀʏ:** `{query}`\n"
        "<blockquote>⚠️ ᴀꜰᴛᴇʀ 15 ᴍɪɴᴜᴛᴇꜱ ᴛʜɪꜱ ᴍᴇꜱꜱᴀɢᴇ ᴡɪʟʟ ʙᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴅᴇʟᴇᴛᴇᴅ 🗑️</blockquote>"
    )
    
    sent_message = await message.reply_text(
        caption,
        reply_markup=InlineKeyboardMarkup(buttons),
        quote=True
    )
    await add_auto_delete_message(sent_message.id, sent_message.chat.id, AUTO_DEL_TIME)


@bot.on_callback_query(filters.regex(r"^series#"))
async def select_series_cb(client, query):
    """Handles selection of a Series Title, and presents Seasons."""
    series_search_title = query.data.split("#")[1]
    
    # 1. Get all seasons for the selected series
    seasons = await db_handler.get_series_seasons(series_search_title)
    
    if not seasons:
        return await query.answer("No seasons found for this series.", show_alert=True)
    
    # 2. Generate buttons for season selection
    buttons = []
    # Create buttons in rows of 4
    row = []
    for season in seasons:
        row.append(InlineKeyboardButton(
            f"Season {season}",
            callback_data=f"season#{series_search_title}#{season}#0" # title#season#offset
        ))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # 3. Edit message to show season buttons
    try:
        await query.edit_message_text(
            f"**Please select a Season for:** `{series_search_title.replace(' ', ' ').title()}`",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except MessageNotModified:
        pass # Ignore if the message is already the same
    
    await query.answer()


@bot.on_callback_query(filters.regex(r"^season#"))
async def select_season_cb(client, query):
    """Handles selection of a Season, and presents Episodes."""
    try:
        _, series_search_title, season_str, offset_str = query.data.split("#")
        season = int(season_str)
        offset = int(offset_str)
    except ValueError:
        return await query.answer("Invalid callback data format.", show_alert=True)
    
    # 1. Get all episodes for the selected series and season
    episodes, next_offset = await db_handler.get_season_episodes(series_search_title, season, offset)
    
    if not episodes:
        return await query.answer("No episodes found for this season/page.", show_alert=True)
    
    # 2. Generate buttons for episode selection
    buttons = []
    for file in episodes:
        buttons.append([InlineKeyboardButton(
            f"Episode {file['episode']:02d} | {file['file_name']}",
            callback_data=f"file#{file['file_id']}"
        )])

    # 3. Add navigation buttons
    nav_buttons = []
    if offset > 0:
        prev_offset = offset - MAX_BUTTONS_PER_PAGE
        nav_buttons.append(InlineKeyboardButton("⏮ Back", callback_data=f"season#{series_search_title}#{season}#{prev_offset}"))
        
    if next_offset is not None:
        nav_buttons.append(InlineKeyboardButton("Next ⏭", callback_data=f"season#{series_search_title}#{season}#{next_offset}"))

    if nav_buttons:
        buttons.append(nav_buttons)
        
    # 4. Add back to series button
    buttons.append([InlineKeyboardButton("⬅️ Back to Series Selection", callback_data=f"back_series#{series_search_title}")])

    # 5. Edit message to show episode buttons
    try:
        current_page = (offset // MAX_BUTTONS_PER_PAGE) + 1
        await query.edit_message_text(
            f"**Series:** `{series_search_title.replace(' ', ' ').title()}`\n"
            f"**Season:** `{season}` | **Page:** `{current_page}`\n\n"
            "**Pʟᴇᴀꜱᴇ Sᴇʟᴇᴄᴛ An Eᴘɪsᴏᴅᴇ Bᴇʟᴏᴡ:**",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except MessageNotModified:
        pass
        
    await query.answer()


@bot.on_callback_query(filters.regex(r"^file#"))
async def send_file_cb(client: Client, query):
    """Handles selection of an Episode, and sends the file."""
    file_id = query.data.split("#")[1]
    
    # Check if the user is authorized to receive the file (optional check)
    # The logic here is simple: if the file exists, send it.

    file_data = await db_handler.get_file_by_id(file_id)

    if not file_data:
        return await query.answer("File not found in database!", show_alert=True)
        
    try:
        # 1. Send the file using its file_id
        await client.send_document(
            chat_id=query.from_user.id,
            document=file_data['file_id'],
            caption=f"**{file_data['file_name']}**"
        )
        await query.answer("File sent to your DMs!", show_alert=True)
    except Exception as e:
        print(f"Error sending file: {e}")
        await query.answer("Failed to send file. Check if the bot has access to the original file.", show_alert=True)

# Main entry point
if __name__ == "__main__":
    print("Bot is starting...")
    bot.run()
