import logging
import asyncio
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember, Message
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TimedOut
from database import save_user_for_broadcast, add_series_file, get_all_broadcast_users, init_db, search_series, get_unique_seasons_by_name, get_all_files_by_season, save_generic_file
from utils import is_series_file, parse_series_info
from config import BOT_TOKEN, LOG_LEVEL, REQUIRED_CHANNEL, INDEX_CHANNEL_ID, ADMINS, LOG_CHANNEL


# --- Global State Management (Replacing pyrogram's `utils.temp`) ---
class BotState:
    def __init__(self):
        self.CANCEL = False
        self.CURRENT = 0
        self.INDEX_LOCK = asyncio.Lock() # Lock for concurrent indexing processes

temp = BotState()


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
## 🔒 Force Subscribe Middleware (No change) ##
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
## 🎬 Command Handlers (No change) ##
###################################################
# ... (start_command, help_command, channel_post_handler - NO CHANGES HERE)

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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a help message."""
    message = (
        "**📚 Bot Commands & Usage:**\n"
        "1. **Search:** Just type the series name (e.g., `Aarya`).\n"
        "2. **/start**: Get a welcome message and register for broadcasts."
        "3. **Indexing (Admins Only):** Forward a file/message link to the bot to request indexing."
        "\n\n**Admin Commands:**\n"
        "4. **/setskip \<number\>**: Sets the starting message ID for full chat indexing."
    )
    await update.message.reply_text(message, parse_mode='Markdown')

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles messages posted in the indexed channel (to save series files)."""
    channel_post = update.channel_post
    # Only index from the configured channel ID
    if str(channel_post.chat.id) != str(INDEX_CHANNEL_ID):
        return

    # Check for Document, Video, or Audio, but prioritize Document for series
    file_media = channel_post.document or channel_post.video or channel_post.audio
    
    if not channel_post or not file_media:
        return 

    file_name = file_media.file_name or channel_post.caption or ""

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
        # Check if the file is a generic media file (e.g., just video/audio/doc not series format)
        if channel_post.video or channel_post.audio or channel_post.document:
            media = channel_post.document or channel_post.video or channel_post.audio
            
            # Prepare data for generic file indexing (similar to Pyrogram's file object)
            file_data = {
                "chat_id": str(channel_post.chat.id),
                "message_id": channel_post.message_id,
                "file_unique_id": media.file_unique_id,
                "file_id": media.file_id,
                "file_name": media.file_name or "",
                "file_size": media.file_size or 0,
                "mime_type": media.mime_type or "",
                "caption": channel_post.caption or "",
                "indexed_at": datetime.utcnow()
            }
            
            # Save the file (ignoring series logic)
            # This is where the generic file saving logic is applied.
            aynav, vnay = save_generic_file(file_data)
            if aynav:
                logging.info(f"Indexed Generic File: {file_data.get('file_name', 'NoName')}")
            # elif vnay == 0: # Duplicate file unique ID
                # logging.debug(f"Duplicate Generic File: {file_data.get('file_name', 'NoName')}")

###################################################
## 🔎 Autofilter Logic Handlers (No change) ##
###################################################
# ... (search_handler, callback_query_handler, show_season_menu, send_season_files)

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
        
    elif action_type == 'index_cancel':
        temp.CANCEL = True
        return await query.answer("Cancelling Indexing")
        
    elif action_type in ['index_accept', 'index_reject']:
        _, chat_id_str, lst_msg_id_str, from_user_id_str = data
        
        chat_id = chat_id_str
        lst_msg_id = int(lst_msg_id_str)
        from_user = int(from_user_id_str)

        if action_type == 'index_reject':
            await query.message.delete()
            await context.bot.send_message(from_user,
                                           f'Your Submission for indexing `{chat_id}` has been declined by our moderators.',
                                           reply_to_message_id=lst_msg_id, parse_mode=ParseMode.MARKDOWN)
            return

        # Handle 'index_accept'
        if temp.INDEX_LOCK.locked():
            return await query.answer('Wait until previous process complete.', show_alert=True)
        
        msg = query.message
        await query.answer('Processing...⏳', show_alert=True)
        
        # Send acceptance message if not an admin
        if from_user not in ADMINS:
            await context.bot.send_message(from_user,
                                           f'Your Submission for indexing `{chat_id}` has been accepted by our moderators and will be added soon.',
                                           reply_to_message_id=lst_msg_id, parse_mode=ParseMode.MARKDOWN)
        
        await msg.edit_text(
            "Starting Indexing",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
            )
        )
        
        # Convert chat_id string to int if it's numeric (channel/group ID)
        try:
            chat_id = int(chat_id)
        except ValueError:
            # It's a username/link, keep it as string
            pass

        # Start the indexing task
        context.job_queue.run_once(
            lambda context: index_files_to_db_wrapper(context.bot, chat_id, lst_msg_id, msg),
            0,
            name=f"index_{chat_id}"
        )

    else:
        await query.edit_message_text(text="Invalid action.")

async def show_season_menu(query, series_name, context):
    """Displays buttons for all unique seasons of the selected series."""
    seasons = get_unique_seasons_by_name(series_name)
    overview = get_series_overview(series_name) 
    
    if not seasons:
        # Use edit_message_text if the original message was text, or send new message
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
        # Check if the message is a photo/caption message or a simple text message to edit
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
## 📝 Indexing Logic Handlers (PyMongo Adaptation) ##
###################################################

async def send_for_index(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles forwarded messages or links for indexing submission."""
    message = update.message
    chat_id = None
    last_msg_id = None

    if message.text:
        regex = re.compile(r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
        match = regex.match(message.text)
        if not match:
            return await message.reply_text('Invalid link')
        
        chat_id_part = match.group(4)
        last_msg_id = int(match.group(5))
        
        if chat_id_part.isnumeric():
            # Private channel link uses 'c/' which maps to -100 + channel_id
            chat_id = int("-100" + chat_id_part)
        else:
            # Public username/link
            chat_id = chat_id_part
            
    elif message.forward_from_chat and message.forward_from_chat.type == ChatType.CHANNEL:
        last_msg_id = message.forward_from_message_id
        chat_id = message.forward_from_chat.username or message.forward_from_chat.id
    else:
        return

    try:
        # Check if the bot can see the chat
        chat_info = await context.bot.get_chat(chat_id)
        # Check if the last message ID is valid
        # This will fail if the bot is not an admin in a private channel/group or if the message doesn't exist
        try:
             await context.bot.get_message(chat_id, last_msg_id)
        except BadRequest:
             return await message.reply_text('Make Sure That Iam An Admin In The Channel/Group, and the message ID is valid.')

    except BadRequest as e:
        if 'chat not found' in str(e) or 'is not a peer' in str(e):
            return await message.reply_text('This may be a private channel / group. Make me an admin over there to index the files.')
        else:
            logging.exception(e)
            return await message.reply_text(f'Errors - {e}')
    except Exception as e:
        logging.exception(e)
        return await message.reply_text(f'Errors - {e}')


    # Convert numeric chat_id back to string for callback data if it's an int
    chat_id_str = str(chat_id)

    # --- Admin Bypass ---
    if message.from_user.id in ADMINS:
        buttons = [
            [
                InlineKeyboardButton('Yes',
                                     callback_data=f'index_accept|{chat_id_str}|{last_msg_id}|{message.from_user.id}')
            ],
            [
                InlineKeyboardButton('close', callback_data='close_data'),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        return await message.reply_text(
            f'Do you Want To Index This Channel/ Group ?\n\nChat ID/ Username: <code>{chat_id_str}</code>\nLast Message ID: <code>{last_msg_id}</code>',
            reply_markup=reply_markup, parse_mode=ParseMode.HTML)


    # --- Non-Admin Request (Requires approval) ---
    # Get invite link (only possible for private chats if bot is admin)
    link = "Private Chat (Admin Required)"
    if isinstance(chat_id, int) and chat_info.type != ChatType.PRIVATE:
        try:
            link = (await context.bot.create_chat_invite_link(chat_id)).invite_link
        except BadRequest:
            link = "Admin required for invite link"
        except Exception as e:
            logging.warning(f"Failed to get invite link for {chat_id}: {e}")
            link = f"Error: {e}"
    elif isinstance(chat_id, str):
        link = f"@{chat_id}"


    buttons = [
        [
            InlineKeyboardButton('Accept Index',
                                 callback_data=f'index_accept|{chat_id_str}|{last_msg_id}|{message.from_user.id}')
        ],
        [
            InlineKeyboardButton('Reject Index',
                                 callback_data=f'index_reject|{chat_id_str}|{message.id}|{message.from_user.id}'),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await context.bot.send_message(LOG_CHANNEL,
                            f'#IndexRequest\n\nBy : {message.from_user.mention} (<code>{message.from_user.id}</code>)\nChat ID/ Username - <code> {chat_id_str}</code>\nLast Message ID - <code>{last_msg_id}</code>\nInviteLink - {link}',
                            reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    
    await message.reply_text('ThankYou For the Contribution, Wait For My Moderators to verify the files.')


async def index_files_to_db_wrapper(bot, chat, lst_msg_id, msg):
    """A wrapper to call the main async indexing function from job_queue."""
    await index_files_to_db(bot, chat, lst_msg_id, msg)

async def index_files_to_db(bot, chat_id, last_msg_id, msg: Message):
    """The main function to iterate and index files from a chat."""
    total_files = 0
    duplicate = 0
    errors = 0
    deleted = 0
    no_media = 0
    unsupported = 0
    
    temp.CURRENT = 0 # Reset current count before starting iteration
    
    async with temp.INDEX_LOCK:
        try:
            current = temp.CURRENT
            temp.CANCEL = False
            
            # Since python-telegram-bot doesn't have an equivalent of pyrogram's iter_messages,
            # we must implement a loop using get_messages with a reverse offset or a simple loop
            # starting from the last message ID (which is more challenging in ptb).
            # The most direct porting approach is to simply loop downwards by message_id.
            
            message_id = last_msg_id
            
            while message_id >= 1 and not temp.CANCEL:
                current += 1
                
                if current % 20 == 0:
                    can = [[InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
                    reply = InlineKeyboardMarkup(can)
                    try:
                        await msg.edit_text(
                            text=f"Total messages fetched: <code>{current}</code>\nTotal messages saved: <code>{total_files}</code>\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>",
                            reply_markup=reply, parse_mode=ParseMode.HTML)
                    except (BadRequest, TimedOut):
                        # Ignore edit message errors, just continue indexing
                        pass
                
                try:
                    message = await bot.get_message(chat_id, message_id)
                except BadRequest as e:
                    if 'message not found' in str(e).lower():
                        deleted += 1
                        message_id -= 1
                        continue
                    else:
                        errors += 1
                        message_id -= 1
                        continue
                except Exception as e:
                    errors += 1
                    logging.exception(f"Error fetching message {message_id}: {e}")
                    message_id -= 1
                    continue
                
                # Check for media types (Document, Video, Audio)
                media = message.document or message.video or message.audio

                if message.empty:
                    deleted += 1
                elif not media:
                    no_media += 1
                elif not (message.document or message.video or message.audio):
                    unsupported += 1
                else:
                    # Logic to save the file
                    # We prioritize series logic first, then fallback to generic save
                    
                    file_name = media.file_name or message.caption or ""
                    
                    if is_series_file(file_name):
                        series_info = parse_series_info(file_name)
                        if series_info:
                            series_name, season, episode = series_info
                            if add_series_file(series_name, season, episode, str(chat_id), message.message_id):
                                total_files += 1
                            else:
                                duplicate += 1
                        else:
                            # It has SXXEYY but parsing failed, treat as unsupported/error
                            unsupported += 1
                    else:
                        # Save as generic file
                        file_data = {
                            "chat_id": str(chat_id),
                            "message_id": message.message_id,
                            "file_unique_id": media.file_unique_id,
                            "file_id": media.file_id,
                            "file_name": media.file_name or "",
                            "file_size": media.file_size or 0,
                            "mime_type": media.mime_type or "",
                            "caption": message.caption or "",
                            "indexed_at": datetime.utcnow()
                        }
                        
                        aynav, vnay = save_generic_file(file_data)
                        if aynav:
                            total_files += 1
                        elif vnay == 0:
                            duplicate += 1
                        elif vnay == 2:
                            errors += 1

                message_id -= 1 # Move to the previous message

            # Post-loop update for cancellation
            if temp.CANCEL:
                await msg.edit_text(f"Successfully Cancelled!!\n\nSaved <code>{total_files}</code> files to dataBase!\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>", parse_mode=ParseMode.HTML)
                return

        except Exception as e:
            logger.exception(e)
            await msg.edit_text(f'Error: {e}')
        else:
            await msg.edit_text(f'Succesfully saved <code>{total_files}</code> to dataBase!\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>', parse_mode=ParseMode.HTML)


async def set_skip_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the starting message ID (skip number) for full chat indexing."""
    if not context.args or len(context.args) != 1:
        return await update.message.reply_text("Usage: /setskip <number>")
    
    try:
        skip = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Skip number should be an integer.")
    
    # Store the skip number in the temporary state
    temp.CURRENT = skip
    await update.message.reply_text(f"Successfully set **SKIP** number as `{skip}`. The next full index will start from message ID {skip}.", parse_mode=ParseMode.MARKDOWN)


###################################################
## ⚙️ Main Bot Setup ##
###################################################

def main() -> None:
    """Start the bot."""
    init_db() # Initialize the database connection
    
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Register Handlers ---

    # Core commands require subscription check
    application.add_handler(CommandHandler("start", check_subscription(start_command)))
    application.add_handler(CommandHandler("help", check_subscription(help_command)))
    
    # Search and Callback handlers require subscription check
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_subscription(search_handler)))
    application.add_handler(CallbackQueryHandler(check_subscription(callback_query_handler)))
    
    # Channel indexing handler (does NOT need subscription check)
    # The filter is updated to include more media types.
    media_filter = filters.Document.ALL | filters.Video.ALL | filters.Audio.ALL
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & media_filter, channel_post_handler))

    # --- New Indexing Handlers (Admin Logic) ---
    admin_filter = filters.User(ADMINS)
    
    # Handle /setskip (Admin only)
    application.add_handler(CommandHandler('setskip', set_skip_number, filters=admin_filter))
    
    # Handle index submission (Forwarded message or Link in private chat)
    index_filter = filters.PRIVATE & (filters.FORWARDED | filters.Regex(r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$"))
    application.add_handler(MessageHandler(index_filter, send_for_index))


    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
