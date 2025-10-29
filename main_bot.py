import logging
import requests 
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
# Corrected database imports (Assuming config is set up correctly for imports)
from database import save_user_for_broadcast, add_series_file, get_all_broadcast_users, init_db, search_series, get_unique_seasons_by_name, get_all_files_by_season
from utils import is_series_file, parse_series_info
from config import BOT_TOKEN, ADMIN_USER_ID, LOG_LEVEL, MAX_SEARCH_RESULTS, REQUIRED_CHANNEL, INDEX_CHANNEL_ID 

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.getLevelName(LOG_LEVEL)
)

# Define a placeholder function
def get_series_overview(series_name):
    """Fetches series overview."""
    logging.info("Poster fetch feature is disabled.")
    return None

###################################################
## 🔒 Force Subscribe Middleware (Corrected) ##
###################################################

def check_subscription(handler):
    """
    Decorator-like function to check if the user is a member of REQUIRED_CHANNEL.
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user:
            return

        user_id = update.effective_user.id
        
        # Admins are always allowed
        if user_id == ADMIN_USER_ID:
            return await handler(update, context)

        try:
            # Check the user's status in the required channel
            chat_member: ChatMember = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
            
            # Allowed statuses: 'member', 'owner', 'administrator'
            if chat_member.status in [ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR]:
                return await handler(update, context) # User is subscribed, run the handler
            else:
                # User is not subscribed or status is 'left', 'kicked', 'restricted'
                channel_link = REQUIRED_CHANNEL.replace('@', '')
                keyboard = [[InlineKeyboardButton("Join Channel 📢", url=f"https://t.me/{channel_link}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.effective_message.reply_text(
                    "🚨 **Subscription Required** 🚨\n\nYou must join our channel to use this bot's search features.",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return
        except Exception as e:
            # Log the error
            logging.error(f"Force sub check failed: {e}")
            await update.effective_message.reply_text("An error occurred while checking your subscription. Please try again.")
            return

    return wrapper
###################################################
## 🎬 Command Handlers ##
###################################################

@check_subscription
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and registers user for broadcast."""
    user_id = update.effective_user.id
    save_user_for_broadcast(user_id)
    
    message = (
        "🎬 Welcome to the Series Indexer Bot!\n\n"
        "To search for a series, simply **type the series name**.\n"
        "The bot will show you the available seasons via buttons."
    )
    await update.message.reply_text(message, parse_mode='Markdown')

@check_subscription
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a help message."""
    message = (
        "**📚 Bot Commands & Usage:**\n"
        "1. **Search:** Just type the series name (e.g., `Aarya`).\n"
        "2. **/start**: Get a welcome message and register for broadcasts.\n"
        "3. **/index_all**: Start indexing ALL files from the source channel (Admin only).\n"
        "4. **/broadcast_test**: Send a test broadcast to all users (Admin only).\n"
    )
    await update.message.reply_text(message, parse_mode='Markdown')

async def index_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Command to start indexing ALL messages from the INDEX_CHANNEL_ID (Admin only).
    Uses robust error handling for message gaps.
    """
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    await update.message.reply_text(f"⏳ **Starting full indexing...**\nIndexing channel messages from `{INDEX_CHANNEL_ID}`. This may take a while. The bot will try to process up to 20,000 messages.", parse_mode='Markdown')
    
    indexed_count = 0
    message_id_counter = 0
    message_id = 1
    MAX_MESSAGE_ID = 20000 
    # Increased to 500 to allow for larger gaps in messages
    MAX_CONSECUTIVE_MISSES = 500 
    consecutive_errors = 0 
    
    try:
        while message_id <= MAX_MESSAGE_ID:
            try:
                # FIX REVERTED: Using context.bot.get_message, which is the standard name.
                # The previous error indicates context.bot does not expose 'get_chat_message'.
                message = await context.bot.get_message(chat_id=INDEX_CHANNEL_ID, message_id=message_id)
                consecutive_errors = 0 # Reset error counter

                channel_post = message
                # Ensure the message is a document (file)
                if channel_post and channel_post.document:
                    file_name = channel_post.document.file_name or channel_post.caption or ""
                    
                    if is_series_file(file_name):
                        series_info = parse_series_info(file_name)
                        if series_info:
                            series_name, season, episode = series_info
                            
                            channel_id = str(channel_post.chat.id)
                            message_id_ = channel_post.message_id
                            
                            # Database check for duplicates is handled inside add_series_file
                            if add_series_file(series_name, season, episode, channel_id, message_id_):
                                indexed_count += 1
                                logging.info(f"Indexed Series File: {series_name} S{season:02d}E{episode:02d}")
                
            except Exception as e:
                # Catch "message not found" (the expected failure for gaps)
                error_msg = str(e)
                if 'message not found' in error_msg.lower() or 'bad request' in error_msg.lower():
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE_MISSES:
                        logging.warning(f"Stopped indexing after {MAX_CONSECUTIVE_MISSES} consecutive failures or critical error.")
                        break # Break out of the while loop
                else:
                    # Logging unhandled errors
                    logging.error(f"Full indexing unhandled error on message {message_id}: {e}")
                    
            message_id += 1 
            message_id_counter = message_id - 1
            
            # Update admin every 1000 messages
            if message_id % 1000 == 0:
                await context.bot.send_message(update.effective_chat.id, f"Progress: Reached message ID **{message_id}**. Indexed **{indexed_count}** files so far.", parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"⚠️ Indexing stopped due to a critical error: `{e}`", parse_mode='Markdown')
        logging.error(f"Full indexing critical error: {e}")

    await update.message.reply_text(f"✅ **Full Indexing Complete!**\nIndexed **{indexed_count}** new files up to message ID **{message_id_counter}**.", parse_mode='Markdown')

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
    """
    Handles messages posted in the indexed channel (to save files).
    """
    channel_post = update.channel_post
    # Only index from the configured channel ID
    if str(channel_post.chat.id) != str(INDEX_CHANNEL_ID):
        return

    if not channel_post or not channel_post.document:
        return 

    file_name = channel_post.document.file_name or channel_post.caption or ""

    if is_series_file(file_name):
        series_info = parse_series_info(file_name)
        if series_info:
            series_name, season, episode = series_info
            
            channel_id = str(channel_post.chat.id)
            message_id = channel_post.message_id
            
            # This is where new files are added if they are not duplicates
            if add_series_file(series_name, season, episode, channel_id, message_id):
                logging.info(f"Indexed Series File: {series_name} S{season:02d}E{episode:02d}")
        else:
            logging.info(f"Ignored Movie/Non-Series File (Post): {file_name}")
    else:
        logging.info(f"Ignored Movie/Non-Series File (Post): {file_name}")

###################################################
## 🔎 Autofilter Logic Handlers ##
###################################################

@check_subscription
async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles user text search queries and initiates the series selection menu."""
    query = update.message.text.strip()
    
    results = search_series(query) 

    if results:
        # Extract unique names from results 
        unique_names = sorted(list(set(name for name, *_, in results)))
        
        keyboard = []
        for name in unique_names:
            callback_data = f'series_select|{name}' 
            keyboard.append([InlineKeyboardButton(name, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"**🔎 Found {len(unique_names)} matching series.** Please select one:"
        await update.message.reply_text(response, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ No series files found matching '{query}'.")

@check_subscription
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button clicks for series and season selection."""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split('|')
    action_type = data[0]
    
    if action_type == 'series_select':
        series_name = data[1]
        await show_season_menu(query, series_name, context)
        
    elif action_type == 'season_send':
        series_name = data[1]
        season = int(data[2])
        await send_season_files(query, series_name, season, context)
    else:
        await query.edit_message_text(text="Invalid action.")

async def show_season_menu(query, series_name, context):
    """Displays buttons for all unique seasons of the selected series."""
    seasons = get_unique_seasons_by_name(series_name)
    overview = get_series_overview(series_name) 
    
    if not seasons:
        await query.edit_message_text(text=f"❌ No seasons found for **{series_name}**.", parse_mode='Markdown')
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
    
    caption = f"📺 **{series_name}**\n\n"
    if overview:
        caption += f"**Plot:** {overview[:250]}..." 
    
    caption += "\n\n**Select a Season:**"
    
    try:
        # Delete the previous message (search result or series list)
        await query.message.delete()
    except Exception:
        pass 

    chat_id = query.message.chat_id
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=caption,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    await query.answer()


async def send_season_files(query, series_name, season, context):
    """Sends all files belonging to the selected season using copy_message (no sender name)."""
    user_chat_id = query.message.chat_id
    files = get_all_files_by_season(series_name, season)
    
    if not files:
        # Check if the message is a photo/caption message or a simple text message to edit
        try:
             await query.edit_message_caption(caption=f"❌ No files found for **{series_name}** Season **{season:02d}**.", parse_mode='Markdown')
        except Exception:
            await query.edit_message_text(text=f"❌ No files found for **{series_name}** Season **{season:02d}**.", parse_mode='Markdown')
        return

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
                # Use copy_message to avoid the "Forwarded from..." tag
                await context.bot.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id
                )
                success_count += 1
            except Exception as e:
                logging.error(f"Failed to copy message {message_id} from {channel_id}: {e}")

    final_message = f"✅ Sent **{success_count}** files for **{series_name}** Season **{season:02d}**."
    await context.bot.send_message(user_chat_id, final_message, parse_mode='Markdown')
    

###################################################
## ⚙️ Main Bot Setup ##
###################################################

async def post_init(application: Application) -> None:
    """Send a restart message to the admin."""
    if ADMIN_USER_ID:
        try:
            await application.bot.send_message(
                chat_id=ADMIN_USER_ID, 
                text="🤖 **Bot Restarted Successfully**", 
                parse_mode='Markdown'
            )
            logging.info("Sent restart message to admin.")
        except Exception as e:
            logging.error(f"Failed to send restart message to admin: {e}")

def main() -> None:
    """Start the bot."""
    init_db() # Initialize the database connection
    
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # --- Register Handlers ---

    # Core commands require subscription check
    application.add_handler(CommandHandler("start", check_subscription(start_command)))
    application.add_handler(CommandHandler("help", check_subscription(help_command)))
    
    # Admin commands (no subscription check needed)
    application.add_handler(CommandHandler("index_all", index_all_command)) 
    application.add_handler(CommandHandler("broadcast_test", broadcast_test_command))

    # Search and Callback handlers require subscription check
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_subscription(search_handler)))
    application.add_handler(CallbackQueryHandler(check_subscription(callback_query_handler)))
    
    # Channel indexing handler 
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.Document.ALL, channel_post_handler))

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
