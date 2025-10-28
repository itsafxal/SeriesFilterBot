from gotgbot.ext import (
    Application, ConversationHandler, MessageHandler, filters, Context, CommandHandler
)
from gotgbot.api import Update
from .db import db, ADMIN_USER_ID

# --- States for ConversationHandler ---
GET_NAME, GET_SEASON, GET_EPISODE, GET_FILE = range(4) 
STATE_LABELS = {
    GET_NAME: "GET_NAME", GET_SEASON: "GET_SEASON", 
    GET_EPISODE: "GET_EPISODE", GET_FILE: "GET_FILE",
    -1: "START"
}

# --- Custom Filter for Conversation State (same as before) ---
class SeriesStateFilter(filters.BaseFilter):
    """Filters updates based on the user's current state in the MongoDB."""
    def __init__(self, expected_state: int):
        self.expected_state = STATE_LABELS[expected_state]
        super().__init__()

    async def filter(self, update: Update) -> bool:
        if not update.effective_message: return False
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        user_state = db.get_state(chat_id, user_id)
        return user_state.get("state") == self.expected_state

    GET_NAME = lambda: SeriesStateFilter(GET_NAME)
    GET_SEASON = lambda: SeriesStateFilter(GET_SEASON)
    GET_EPISODE = lambda: SeriesStateFilter(GET_EPISODE)
    GET_FILE = lambda: SeriesStateFilter(GET_FILE)

# --- Conversation Step Handlers ---

async def add_series_start(update: Update, context: Context) -> int:
    """Entry point for the /addseries command."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.effective_message.reply_text("You are not authorized to use this command.")
        return ConversationHandler.END

    db.save_state(update.effective_chat.id, update.effective_user.id, STATE_LABELS[GET_NAME], {})
    
    await update.effective_message.reply_text(
        "🎬 **Series Addition Started**\n\n"
        "1/4: Please send the **full name of the Series** (e.g., 'Game of Thrones').",
        parse_mode="Markdown"
    )
    return GET_NAME 

async def get_series_name(update: Update, context: Context) -> int:
    series_name = update.effective_message.text.strip()
    user_data = db.get_state(update.effective_chat.id, update.effective_user.id).get("data", {})
    user_data["series_name"] = series_name
    
    db.save_state(update.effective_chat.id, update.effective_user.id, STATE_LABELS[GET_SEASON], user_data)

    await update.effective_message.reply_text(
        f"2/4: Series name accepted: **{series_name}**.\n\n"
        "Now, send the **Season Number** (e.g., 1 or 01).",
        parse_mode="Markdown"
    )
    return GET_SEASON

async def get_series_season(update: Update, context: Context) -> int:
    season_text = update.effective_message.text.strip()
    try:
        season = int(season_text)
        if season < 0: raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Invalid season number. Please enter a positive integer.")
        return GET_SEASON

    user_data = db.get_state(update.effective_chat.id, update.effective_user.id).get("data", {})
    user_data["season"] = season
    
    db.save_state(update.effective_chat.id, update.effective_user.id, STATE_LABELS[GET_EPISODE], user_data)

    await update.effective_message.reply_text(
        f"3/4: Season **{season}** accepted.\n\n"
        "Next, send the **Episode Number** (e.g., 5 or 05).",
        parse_mode="Markdown"
    )
    return GET_EPISODE

async def get_series_episode(update: Update, context: Context) -> int:
    episode_text = update.effective_message.text.strip()
    try:
        episode = int(episode_text)
        if episode <= 0: raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Invalid episode number. Please enter a positive integer.")
        return GET_EPISODE

    user_data = db.get_state(update.effective_chat.id, update.effective_user.id).get("data", {})
    user_data["episode"] = episode
    
    db.save_state(update.effective_chat.id, update.effective_user.id, STATE_LABELS[GET_FILE], user_data)

    await update.effective_message.reply_text(
        f"4/4: Episode **{episode}** accepted.\n\n"
        "Finally, **forward the video file** you want to save. Movies and non-video files will be ignored.",
        parse_mode="Markdown"
    )
    return GET_FILE

async def get_series_file(update: Update, context: Context) -> int:
    message = update.effective_message
    if not message.video:
        await message.reply_text(
            "⚠️ **File Ignored:** That was not a video file. Please forward the video file for the series you are adding, or /cancel."
        )
        return GET_FILE # Stay in the same state
        
    # Ignore files over a certain size if you want to enforce a "no movies" rule, 
    # but the simplest "series-only" distinction is through structured metadata input.
        
    file_id = message.video.file_id
    user_data = db.get_state(message.chat_id, message.from_user.id).get("data", {})
    
    # Final data structure for DB
    series_data = {
        "series_name": user_data["series_name"],
        "season": user_data["season"],
        "episode": user_data["episode"],
        "file_id": file_id,
        "file_unique_id": message.video.file_unique_id,
        "mime_type": message.video.mime_type,
        "date_added": message.date.isoformat()
    }
    
    # Save to MongoDB
    success = db.save_series_file(series_data)
    
    # Clear state and end conversation
    db.clear_state(message.chat_id, message.from_user.id)
    
    if success:
        await message.reply_text(
            f"✅ **Success!**\n"
            f"Saved: **{series_data['series_name']}** S{series_data['season']:02d}E{series_data['episode']:02d}.\n"
            "Series addition complete.",
            parse_mode="Markdown"
        )
    else:
        await message.reply_text(
            "❌ **Error!**\n"
            "Failed to save file. This episode might already exist in the database (duplicate entry)."
        )
        
    return ConversationHandler.END

async def cancel_series(update: Update, context: Context) -> int:
    """Cancels the series addition and clears the state."""
    db.clear_state(update.effective_chat.id, update.effective_user.id) 
    await update.effective_message.reply_text("Series addition cancelled.")
    return ConversationHandler.END

# --- Admin Commands ---

async def start_message(update: Update, context: Context):
    """Sends a friendly start message."""
    await update.effective_message.reply_text(
        "👋 Welcome! I am your Series File Vault Bot.\n"
        "To start a series addition, use the /addseries command (Admin only).\n"
        "For content search, simply type the name of the series."
    )

async def admin_stats(update: Update, context: Context):
    """Shows series statistics."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.effective_message.reply_text("🚫 You are not authorized to view stats.")
        return

    stats = db.get_stats()
    
    # Database Space: MongoDB does not expose "remaining space" easily. 
    # We report the collection count as the primary metric.
    
    await update.effective_message.reply_text(
        "📊 **Bot Statistics**\n"
        "---------------------------\n"
        f"🔗 DB Status: **{stats['db_status']}**\n"
        f"📺 Total **Series** Added: **{stats['total_series']}**\n"
        f"🎬 Total **Episodes** Stored: **{stats['total_series_episodes']}**\n"
        "📝 *Note: Database usage (space) is managed at the system level. The number above is the document count.*",
        parse_mode="Markdown"
    )

async def admin_restart_message(update: Update, context: Context):
    """Sends a restart message (simulated)."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.effective_message.reply_text("🚫 Only the Admin can simulate a restart message.")
        return

    await update.effective_message.reply_text(
        "🔄 **Bot Restarted**\n\n"
        "The bot process has been successfully restarted. The MongoDB conversation states are preserved.\n"
        "New updates are being processed.",
        parse_mode="Markdown"
    )

# --- ConversationHandler Instance ---

series_handler = ConversationHandler(
    entry_points=[
        CommandHandler("addseries", add_series_start) 
    ],
    states={
        GET_NAME: [
            MessageHandler(SeriesStateFilter.GET_NAME() & filters.TEXT & (~filters.COMMAND), get_series_name)
        ],
        GET_SEASON: [
            MessageHandler(SeriesStateFilter.GET_SEASON() & filters.TEXT & (~filters.COMMAND), get_series_season)
        ],
        GET_EPISODE: [
            MessageHandler(SeriesStateFilter.GET_EPISODE() & filters.TEXT & (~filters.COMMAND), get_series_episode)
        ],
        GET_FILE: [
            # Only proceed if the message contains a video file
            MessageHandler(SeriesStateFilter.GET_FILE() & (filters.VIDEO | filters.TEXT), get_series_file)
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_series)
    ],
    per_user=True,  
    per_chat=True,
    allow_reentry=True
)

# --- Other Handlers to be added in main.py ---
other_handlers = [
    CommandHandler("start", start_message),
    CommandHandler("stats", admin_stats),
    CommandHandler("restartmsg", admin_restart_message) # Simulated admin restart confirmation
]
