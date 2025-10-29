import logging
import asyncio
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember, Message
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TimedOut
from datetime import datetime
from database import (
    save_user_for_broadcast, 
    init_db, 
    search_series, 
    get_unique_seasons_by_name, 
    get_all_files_by_season, 
    # Removed: add_series_file, get_all_broadcast_users, save_generic_file
)
from config import BOT_TOKEN, LOG_LEVEL, REQUIRED_CHANNEL, ADMINS
# Removed: INDEX_CHANNEL_ID, LOG_CHANNEL

# --- Global State Management (Indexing state removed) ---
# Removed BotState class and 'temp' global state as they are only used for indexing.


# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.getLevelName(LOG_LEVEL)
)

# Define a placeholder function
def get_series_overview(series_name):
    """Fetches series overview (Placeholder)."""
    logging.info("Poster fetch feature is disabled.")
    return None

###################################################
## 🔒 Force Subscribe Middleware ##
###################################################

def check_subscription(handler):
    """
    Decorator-like function to check if the user is a member of REQUIRED_CHANNEL.
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user:
            return

        # Use effective_message/callback_query to check if we should perform the sub-check
        if not update.effective_message and not update.callback_query:
            return await handler(update, context)

        user_id = update.effective_user.id
        
        # Admin bypass (assuming ADMINS list is defined)
        if user_id in ADMINS:
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
                
                # Determine where to send the requirement message
                if update.callback_query:
                    # For callbacks, use answer and send a new message
                    await update.callback_query.answer(text="Subscription Required!", show_alert=True)
                    target_chat = update.callback_query.message.chat_id
                else:
                    # For messages, reply to the message
                    target_chat = update.effective_chat.id
                
                await context.bot.send_message(
                    chat_id=target_chat,
                    text="🚨 **Subscription Required** 🚨\n\nYou must join our channel to use this bot's search features.",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return
        except Exception as e:
            # Log the error
            logging.error(f"Force sub check failed: {e}")
            if update.effective_message:
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
        # Removed indexing mention
    )
    await update.message.reply_text(message, parse_mode='Markdown')

# Removed: async def channel_post_handler(...) - No automatic file indexing

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
        
    # Removed: 'index_cancel', 'index_accept', 'index_reject' logic
        
    else:
        await query.edit_message_text(text="Invalid action.")

async def show_season_menu(query, series_name, context):
    """Displays buttons for all unique seasons of the selected series."""
    seasons = get_unique_seasons_by_name(series_name)
    overview = get_series_overview(series_name) 
    
    if not seasons:
        try:
              await query.message.edit_text(text=f"❌ No seasons found for **{series_name}**.", parse_mode='Markdown')
        except Exception:
              await context.bot.send_message(query.message.chat_id, text=f"❌ No seasons found for **{series_name}**.", parse_mode='Markdown')
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
    
    chat_id = query.message.chat_id
    
    # Attempt to delete the previous message (search result or series list)
    try:
        await query.message.delete()
    except Exception:
        pass 
        
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
        try:
              await query.message.edit_text(text=f"❌ No files found for **{series_name}** Season **{season:02d}**.", parse_mode='Markdown')
        except Exception:
              await context.bot.send_message(user_chat_id, text=f"❌ No files found for **{series_name}** Season **{season:02d}**.", parse_mode='Markdown')
        return

    # Send a status message (since the previous message was deleted)
    status_message = await context.bot.send_message(
        user_chat_id,
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
    
    # Edit the status message with the final count
    try:
        await status_message.edit_text(final_message, parse_mode='Markdown')
    except Exception:
        await context.bot.send_message(user_chat_id, final_message, parse_mode='Markdown')


###################################################
## 📝 Indexing Logic Handlers (Removed) ##
###################################################
# Removed: send_for_index, index_files_to_db_wrapper, index_files_to_db, set_skip_number


def main() -> None:
    """Start the bot."""
    init_db() # Initialize the database connection
    
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Register Handlers ---

    # Core commands require subscription check
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    
    # Search and Callback handlers require subscription check
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, search_handler))
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    
    # Removed: Channel indexing handler
    # Removed: Admin commands (/setskip)
    # Removed: Index submission handler

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
