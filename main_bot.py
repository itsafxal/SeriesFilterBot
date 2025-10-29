# main_bot.py

import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from database import save_user_for_broadcast, search_series, add_series_file, get_all_broadcast_users, get_file_details_by_file_id
from utils import is_series_file, parse_series_info

# --- Configuration Loading from config.py ---
from config import BOT_TOKEN, ADMIN_USER_ID, LOG_LEVEL, MAX_SEARCH_RESULTS

# Set up logging using the variable (ensuring it matches the config in database.py)
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
    """Handles user text search queries and forwards the file if found."""
    query = update.message.text.strip()
    user_chat_id = update.message.chat_id
    
    results = search_series(query) 

    if results:
        # Get the file_id from the top result
        name, season, episode, file_id = results[0] 
        
        # 1. Send text response first
        response = f"**🔎 Found {len(results)} matches.** Showing top {min(len(results), MAX_SEARCH_RESULTS)}:\n"
        
        # List the results based on the MAX_SEARCH_RESULTS limit
        for i, (res_name, res_season, res_episode, _) in enumerate(results[:MAX_SEARCH_RESULTS]):
             response += f'🔸 {res_name} - S{res_season:02d}E{res_episode:02d}\n'

        response += "\nSending the best result now..."
        await update.message.reply_text(response, parse_mode='Markdown')
        
        # 2. Retrieve Channel and Message ID using the file_id of the best match
        full_entry = get_file_details_by_file_id(file_id)
        
        if full_entry:
            channel_id = full_entry.get('channel_id')
            message_id = full_entry.get('message_id')
            
            if channel_id and message_id:
                try:
                    # Forward the original message from the channel to the user
                    await context.bot.forward_message(
                        chat_id=user_chat_id,
                        from_chat_id=channel_id,
                        message_id=message_id
                    )
                except Exception as e:
                    logging.error(f"Failed to forward message {message_id} from {channel_id}: {e}")
                    await update.message.reply_text("❌ Error sending the file. The original message may be inaccessible or deleted.")
            else:
                await update.message.reply_text("❌ File metadata missing. Cannot retrieve.")
    else:
        await update.message.reply_text(f"❌ No series files found matching '{query}'.")


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
    # Use the BOT_TOKEN imported from config.py
    application = Application.builder().token(BOT_TOKEN).build()

    # Register Handlers
    # Register Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("index", index_command))
    application.add_handler(CommandHandler("broadcast_test", broadcast_test_command))
    # ... (other handlers) ...

    # User chat search handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))
    
    # Corrected Channel indexing handler: Use filters.Document.ALL
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.Document.ALL, channel_post_handler))

    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
