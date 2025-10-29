import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from database import save_user_for_broadcast, add_series_file, get_all_broadcast_users, get_file_details_by_file_id
# ⬇️ Import new functions
from database import search_series, get_unique_seasons_by_name, get_all_files_by_season
from utils import is_series_file, parse_series_info
from config import BOT_TOKEN, ADMIN_USER_ID, LOG_LEVEL, MAX_SEARCH_RESULTS

# Logging is configured in database.py, but we ensure the level is set here too for consistency
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.getLevelName(LOG_LEVEL)
)

# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command and saves user for broadcast."""
    user_id = update.message.from_user.id
    user_name = update.message.from_user.full_name

    save_user_for_broadcast(user_id)
    
    await update.message.reply_text(
        f'👋 Welcome, **{user_name}**! I am your professional series autofilter bot. '
        'I index series files only. Start searching by typing the series name and episode!',
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command."""
    help_message = (
        '**🤖 Bot Commands & Usage:**\n\n'
        '**/start** - Start the bot and save your ID for updates.\n'
        '**/help** - Show this help message.\n'
        '**/index** - *Admin Only:* Check indexing status.\n\n'
        '**🔎 Pro Search Usage:**\n'
        'Type: `[Series Name] s[Season]e[Episode]` (e.g., `The Flash s05e12`).'
    )
    await update.message.reply_text(help_message, parse_mode='Markdown')

async def index_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder for an admin indexing command."""
    await update.message.reply_text('New files are automatically indexed from the connected channel. This command is a placeholder.')


# --- Message Handlers (Autofilter/Indexing) ---

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles new posts in a linked channel for automatic indexing."""
    post = update.channel_post
    # Only process documents (files)
    if not post or not post.document:
        return

    file_name = post.document.file_name
    file_id = post.document.file_id
    channel_id = str(post.chat_id)
    message_id = post.message_id 
    
    if is_series_file(file_name):
        series_info = parse_series_info(file_name)
        
        if series_info:
            series_name, season, episode = series_info
            
            # Index the file using the robust (channel_id, message_id) pair
            success = add_series_file(series_name, season, episode, file_id, channel_id, message_id)

            if success:
                logging.info(f"Indexed Series: {series_name} S{season}E{episode}. Msg ID: {message_id}")
            else:
                 logging.warning(f"Duplicate file ignored: {file_name}")
        else:
             logging.info(f"File marked as series but couldn't parse: {file_name}")
    else:
        logging.info(f"Ignored Movie/Non-Series File: {file_name}")


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
        await show_season_menu(query, series_name)
        
    elif action_type == 'season_send':
        # --- STEP 3: Send All Files for Season ---
        series_name = data[1]
        season = int(data[2])
        await send_season_files(query, series_name, season, context)
    else:
        await query.edit_message_text(text="Invalid action.")


async def show_season_menu(query, series_name):
    """Displays buttons for all unique seasons of the selected series."""
    seasons = get_unique_seasons_by_name(series_name)
    
    if not seasons:
        await query.edit_message_text(text=f"❌ No seasons found for {series_name}.")
        return

    keyboard = []
    # Create buttons in rows of 5
    row = []
    for season in seasons:
        # Callback data format: type|series_name|season_number
        callback_data = f'season_send|{series_name}|{season}'
        button_text = f"Season {season:02d}"
        row.append(InlineKeyboardButton(button_text, callback_data=callback_data))
        
        if len(row) >= 5: # Arrange in max 5 buttons per row
            keyboard.append(row)
            row = []
    
    if row: # Add the last row if not empty
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=f"📺 **{series_name}**\n\nPlease select a season to receive all files:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def send_season_files(query, series_name, season, context):
    """Sends all files belonging to the selected season."""
    user_chat_id = query.message.chat_id
    files = get_all_files_by_season(series_name, season)
    
    if not files:
        await query.edit_message_text(f"❌ No files found for {series_name} Season {season:02d}.")
        return

    # Acknowledge the request before sending large number of files
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
                # Do not stop, just log the error

    final_message = f"✅ Sent **{success_count}** files for **{series_name}** Season **{season:02d}**."
    await context.bot.send_message(user_chat_id, final_message, parse_mode='Markdown')

# --- Broadcast Example (Admin Only) ---

async def broadcast_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tests the broadcast function (Admin Only)."""
    user_id = update.message.from_user.id
    
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("🚫 Access Denied: You are not the admin.")
        return

    users = get_all_broadcast_users()
    if not users:
        await update.message.reply_text("No users saved for broadcast.")
        return

    broadcast_message = "📢 **New Series Alert!** This is a test broadcast!"
    
    success_count = 0
    for target_id in users:
        try:
            await context.bot.send_message(target_id, broadcast_message, parse_mode='Markdown')
            success_count += 1
        except Exception as e:
            logging.error(f"Failed to send broadcast to user {target_id}: {e}")

    await update.message.reply_text(f"Broadcast sent successfully to {success_count}/{len(users)} users.")


# main_bot.py

# ... (main function body) ...

def main() -> None:
    """Start the bot."""
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
