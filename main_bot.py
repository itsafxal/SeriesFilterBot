# main_bot.py

import logging
import requests # Needed for API calls (though not for poster anymore)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from database import save_user_for_broadcast, add_series_file, get_all_broadcast_users, get_file_details_by_file_id
from database import search_series, get_unique_seasons_by_name, get_all_files_by_season
from utils import is_series_file, parse_series_info
# REMOVED TMDB_API_KEY from config import
from config import BOT_TOKEN, ADMIN_USER_ID, LOG_LEVEL, MAX_SEARCH_RESULTS 
from database import init_db # Import init_db

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.getLevelName(LOG_LEVEL)
)

# --- TMDB Helper Function (REMOVED) ---
# The original get_series_poster function is now removed.

# Define a placeholder function to replace it, returning None
def get_series_overview(series_name):
    """Fetches series overview. (Note: TMDB API key is still needed for this feature if you wanted it)"""
    # Simplified placeholder for series overview without a dedicated API
    # You could re-implement this with OMDb or another service if needed, 
    # but for now, we'll just return None to keep it simple.
    logging.info("Poster fetch feature is disabled.")
    return None

# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and registers user for broadcast."""
    user_id = update.effective_user.id
    save_user_for_broadcast(user_id)
    
    message = (
        "🎬 Welcome to the Series Indexer Bot!\n\n"
        "To search for a series, simply **type the series name**.\n"
        "The bot will show you the available seasons via buttons."
    )
    await update.message.reply_text(message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a help message."""
    message = (
        "**📚 Bot Commands & Usage:**\n"
        "1. **Search:** Just type the series name (e.g., `Aarya`).\n"
        "2. **/start**: Get a welcome message and register for broadcasts.\n"
        "3. **/index**: Start indexing the linked channel messages (Admin only).\n"
        "4. **/broadcast_test**: Send a test broadcast to all users (Admin only).\n"
    )
    await update.message.reply_text(message, parse_mode='Markdown')

async def index_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command to start indexing from a private channel (Admin only)."""
    # NOTE: Channel indexing logic (not fully provided, but placeholder is here)
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    await update.message.reply_text("Indexing logic would start here, processing channel messages.")


async def broadcast_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a test broadcast to all users (Admin only)."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    users = get_all_broadcast_users()
    if not users:
        await update.message.reply_text("No users registered for broadcast.")
        return

    test_message = "📢 **Broadcast Test!** This is a test message from the admin."
    count = 0
    for user_id in users:
        try:
            await context.bot.send_message(user_id, test_message, parse_mode='Markdown')
            count += 1
        except Exception as e:
            logging.error(f"Failed to send broadcast to user {user_id}: {e}")

    await update.message.reply_text(f"✅ Sent test broadcast to {count} users.")


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles messages posted in the indexed channel (to save files)."""
    channel_post = update.channel_post
    if not channel_post or not channel_post.document:
        return 

    # Use the Telegram document's file_name or caption for parsing
    file_name = channel_post.document.file_name or channel_post.caption or ""
    file_name = file_name.replace(' ', '.') # Normalize spacing for better regex match

    if is_series_file(file_name):
        series_info = parse_series_info(file_name)
        if series_info:
            series_name, season, episode = series_info
            
            # The file_id is required to forward the file later
            file_id = channel_post.document.file_id
            channel_id = str(channel_post.chat.id)
            message_id = channel_post.message_id
            
            if add_series_file(series_name, season, episode, file_id, channel_id, message_id):
                logging.info(f"Indexed Series File: {series_name} S{season:02d}E{episode:02d}")
            else:
                logging.warning(f"Duplicate file ignored: {series_name} S{season:02d}E{episode:02d}")
        else:
             logging.info(f"Ignored Movie/Non-Series File: {file_name}")
    else:
        logging.info(f"Ignored Movie/Non-Series File: {file_name}")

# ----------------------------------------------------
# 📌 AUTOFILTER LOGIC: Search -> Series Menu -> Season Menu -> Send Files
# ----------------------------------------------------

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles user text search queries and initiates the series selection menu."""
    query = update.message.text.strip()
    
    # Use search_series to find matches, but we only care about unique names for the menu
    results = search_series(query) 

    if results:
        # Get unique series names from the results, preserving title case
        unique_names = sorted(list(set(name for name, *_, in results)))
        
        # Build the inline keyboard for Series Selection
        keyboard = []
        for name in unique_names:
            # Callback data format: type|series_name
            callback_data = f'series_select|{name}' 
            keyboard.append([InlineKeyboardButton(name, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"**🔎 Found {len(unique_names)} matching series.** Please select one:"
        await update.message.reply_text(response, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ No series files found matching '{query}'.")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button clicks for series and season selection."""
    query = update.callback_query
    await query.answer()
    
    # Data is split into: [type, value1, value2]
    data = query.data.split('|')
    action_type = data[0]
    
    if action_type == 'series_select':
        # --- STEP 2: Show Season Menu ---
        series_name = data[1]
        await show_season_menu(query, series_name, context)
        
    elif action_type == 'season_send':
        # --- STEP 3: Send All Files for Season ---
        series_name = data[1]
        season = int(data[2])
        await send_season_files(query, series_name, season, context)
    else:
        await query.edit_message_text(text="Invalid action.")


async def show_season_menu(query, series_name, context):
    """Displays buttons for all unique seasons of the selected series. (Poster functionality removed)"""
    seasons = get_unique_seasons_by_name(series_name)
    
    # POSTER/OVERVIEW FETCHING LINE REMOVED: poster_url, overview = get_series_poster(series_name)
    # Replaced with a simplified call to maintain logic flow if you later re-add a basic overview:
    overview = get_series_overview(series_name) 
    
    # 1. Prepare Keyboard
    if not seasons:
        await query.edit_message_text(text=f"❌ No seasons found for **{series_name}**.", parse_mode='Markdown')
        return

    keyboard = []
    # Create buttons in rows of 5
    row = []
    for season in seasons:
        # Callback data format: type|series_name|season_number
        callback_data = f'season_send|{series_name}|{season}'
        button_text = f"S{season:02d}"
        row.append(InlineKeyboardButton(button_text, callback_data=callback_data))
        
        if len(row) >= 5: # Arrange in max 5 buttons per row
            keyboard.append(row)
            row = []
    
    if row: # Add the last row if not empty
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 2. Prepare Message Text
    caption = f"📺 **{series_name}**\n\n"
    if overview:
        # Add plot summary if available
        caption += f"**Plot:** {overview[:250]}..." # Truncate plot summary for caption
    
    caption += "\n\n**Select a Season:**"
    
    # 3. Send Text (No Poster/Photo logic needed anymore)
    # Delete the previous button message (the series name selection)
    try:
        await query.message.delete()
    except Exception:
        pass # Ignore if message can't be deleted (e.g., if it's the only message)

    chat_id = query.message.chat_id
    
    # SIMPLIFIED: ALWAYS send a text message as the fallback is now the main path
    await context.bot.send_message(
        chat_id=chat_id,
        text=caption,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    # REMOVED the 'if poster_url:' block entirely
    
    await query.answer() # Final answer to the query


async def send_season_files(query, series_name, season, context):
    """Sends all files belonging to the selected season."""
    user_chat_id = query.message.chat_id
    files = get_all_files_by_season(series_name, season)
    
    if not files:
        await query.edit_message_caption(caption=f"❌ No files found for **{series_name}** Season **{season:02d}**.", parse_mode='Markdown')
        return

    # Acknowledge the request before sending large number of files
    # NOTE: edit_message_caption is used because the previous message was a photo with a caption.
    # Now that it's a text message, you should use edit_message_text, but I'll stick to 
    # edit_message_caption here for compatibility in case you revert the previous step.
    # A safer approach is to use try/except for both edit_message_caption and edit_message_text.
    try:
        await query.edit_message_caption(
            caption=f"📦 Sending **{len(files)} files** for **{series_name}** Season **{season:02d}**...",
            parse_mode='Markdown'
        )
    except Exception:
        await query.edit_message_text(
            text=f"📦 Sending **{len(files)} files** for **{series_name}** Season **{season:02d}**...",
            parse_mode='Markdown'
        )


    success_count = 0
    for file_entry in files:
        channel_id = file_entry.get('channel_id')
        message_id = file_entry.get('message_id')
        
        if channel_id and message_id:
            try:
                await context.bot.forward_message(
                    chat_id=user_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id
                )
                success_count += 1
            except Exception as e:
                logging.error(f"Failed to forward message {message_id} from {channel_id}: {e}")

    final_message = f"✅ Sent **{success_count}** files for **{series_name}** Season **{season:02d}**."
    await context.bot.send_message(user_chat_id, final_message, parse_mode='Markdown')
    

# ----------------------------------------------------
# 📌 Main Bot Setup
# ----------------------------------------------------

def main() -> None:
    """Start the bot."""
    init_db() # Initialize the database connection
    
    application = Application.builder().token(BOT_TOKEN).build()

    # Register Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("index", index_command))
    application.add_handler(CommandHandler("broadcast_test", broadcast_test_command))

    # 1. Search Handler (Initiates the menu)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))
    
    # 2. Callback Query Handler (Manages button clicks for Series and Seasons)
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    
    # Channel indexing handler 
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.Document.ALL, channel_post_handler))

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
