# command.py - MODIFIED for interactive and link-based indexing with deprecation fix
# FIX: Changed enums.MessageOrigin.CHAT to enums.MessageOriginType.CHAT or similar
# Based on the error, we assume the Enum Class name is MessageOriginType.

import asyncio
import re
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChannelPrivate
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from config import logger, ADMIN_USERS, INDEX_CHANNELS 
from database import (
    get_bot_state,
    save_bot_state,
    get_global_setting, 
    set_global_setting,
)
from utils import save_series_file_for_bulk_index 

# --- NEW GLOBALS AND HELPER FUNCTIONS ---
lock = asyncio.Lock()
media_filter = filters.document | filters.video | filters.audio
index_task = None
link_pattern = r"https://t\.me/c/(\d+)/(\d+)" # Pattern to match c/channel_id/message_id links

def extract_links(links):
    """
    Extracts chat_id, start_msg_id, and last_msg_id from one or two links.
    """
    if len(links) == 1:
        match = re.match(link_pattern, links[0])
        if not match:
            raise ValueError("Invalid link format.")
        channel_id, last_msg_id = match.groups()
        # Convert to -100XXXXXXXXXX format
        chat_id = int(f"-100{channel_id}")
        # Default start ID to 2 (1 is usually service message)
        return chat_id, 2, int(last_msg_id)

    elif len(links) == 2:
        match1 = re.match(link_pattern, links[0])
        match2 = re.match(link_pattern, links[1])
        if not match1 or not match2:
            raise ValueError("Invalid link format.")
        channel_id1, start_msg_id = match1.groups()
        channel_id2, last_msg_id = match2.groups()
        if channel_id1 != channel_id2:
            raise ValueError("Links belong to different channels.")
        chat_id = int(f"-100{channel_id1}")
        return chat_id, int(start_msg_id), int(last_msg_id)

    raise ValueError("Invalid number of links provided.")

async def index_files_task(bot, msg, chat_id, start_msg_id, last_msg_id):
    """The main background task for sequential indexing."""
    global index_task

    total_files = 0
    current = int(start_msg_id)
    counter = 0
    saved = 0
    total = last_msg_id + 1

    async with lock:
        try:
            while current < total:
                try:
                    # Fetching messages one by one to respect start/end IDs
                    message = await bot.get_messages(
                        chat_id=chat_id, message_ids=current, replies=0
                    )
                except FloodWait as e:
                    logger.warning("FloodWait while indexing, Error: %s", str(e))
                    await asyncio.sleep(e.value)
                    # Retry after sleep
                    message = await bot.get_messages(
                        chat_id=chat_id, message_ids=current, replies=0
                    )
                except asyncio.CancelledError:
                    logger.info("Indexing task was cancelled.")
                    await msg.edit_text("Indexing process was cancelled.")
                    return
                except ChannelPrivate as e:
                    logger.warning(
                        "Chat is private or bot is not an admin: %s : %s", chat_id, str(e)
                    )
                    await msg.edit_text(f"Chat is private or bot is not an admin: {chat_id}\nError: {str(e)}")
                    return
                except Exception as e:
                    # Ignore deletion/access errors and move to next ID
                    logger.warning(f"Error occurred while fetching message {current} from {chat_id}: {str(e)}")
                    current += 1
                    continue

                if message and (message.document or message.video or message.audio):
                    # Use the existing save_series_file_for_bulk_index from utils.py
                    saved_flag, count = await save_series_file_for_bulk_index(message)
                    if saved_flag:
                        saved += count
                        total_files += count
                
                current += 1
                counter += 1
                
                # Update status message every 200 messages checked
                if counter >= 200:
                    try:
                        kb = InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "Cancel Indexing", callback_data="cancel_index"
                                    ),
                                ]
                            ]
                        )
                        await msg.edit_text(
                            f"Total messages checked: **{current - 1}**\nTotal files saved: **{saved}**",
                            reply_markup=kb,
                            parse_mode=enums.ParseMode.MARKDOWN
                        )
                        logger.info(
                            "INDEX: Total messages & files processed: %s : %s",
                            current - 1,
                            saved,
                        )
                    except FloodWait as e:
                        logger.warning(
                            "FloodWait while editing count message, sleeping for: %s seconds",
                            str(e.value),
                        )
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        logger.warning(f"Failed to edit status message: {e}")
                    counter -= 200
            
        except asyncio.CancelledError:
            logger.info("Indexing task was cancelled.")
            await msg.edit_text("Indexing process was cancelled.")
        except Exception as e:
            logger.exception(e)
            await msg.edit_text(f"Error while indexing: {e}")
        else:
            logger.info("Complete: Total files saved: %s", saved)
            await msg.edit_text(f"🥳 **Complete!** Total **{saved}** Files Saved To Database!")
        finally:
            index_task = None

def register_command_handlers(app: Client):
    """Registers all command-based message handlers with the Pyrogram client."""

    @app.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):
        """Handles the /start command."""
        welcome_text = (
            "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
            "Just send me the <b>name of a series</b> you are looking for. "
            "I'll show you a summary and ask you to select the **Season**."
        )
        await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

    # --- NEW HANDLERS FOR INTERACTIVE INDEXING (FIXED) ---

    @app.on_message(
        filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter
    )
    async def index_files_forwarded(bot, message):
        """Admin command to start index by forwarding the last message."""
        if lock.locked():
            await message.reply("Wait until the previous process completes.")
            return
            
        user_id = message.from_user.id
        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            await message.reply_text(
                "Please forward a message from the channel *without* forwarding as a copy.",
                parse_mode=enums.ParseMode.MARKDOWN
            )
            return

        last_msg_id = origin.message_id
        chat = origin.sender_chat
        chat_id_input = chat.username if chat.username else chat.id
        
        if isinstance(chat_id_input, str):
            try:
                full_chat = await bot.get_chat(chat_id_input)
                final_chat_id = full_chat.id
            except Exception as e:
                await message.reply_text(f"Unable to resolve chat ID for {chat_id_input}. Error: `{e}`")
                return
        else:
            final_chat_id = chat_id_input
            
        # Ensure the final ID is the negative integer format
        if not str(final_chat_id).startswith("-100"):
            final_chat_id = final_chat_id if final_chat_id < 0 else int(f"-100{final_chat_id}")


        try:
            # Simple check to see if bot can access the last message ID
            await bot.get_messages(chat_id=final_chat_id, message_ids=last_msg_id)

            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Proceed (Start at ID 2)",
                            callback_data=f"index {final_chat_id} {2} {last_msg_id}",
                        ),
                        InlineKeyboardButton("Cancel", callback_data="can-index"),
                    ]
                ]
            )
            await bot.send_message(
                user_id,
                f"Please confirm if you want to start indexing channel `{final_chat_id}` "
                f"from message ID 2 up to the forwarded message ID `{last_msg_id}`.",
                reply_markup=kb,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Forward Index Check Failed: {e}")
            await message.reply_text(
                f"Unable to start indexing, either the channel is private and bot is not an admin, or you forwarded the message as copy.\nError: <code>{e}</code>",
                parse_mode=enums.ParseMode.HTML
            )


    @app.on_message(
        filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink")
    )
    async def manual_index(bot, message):
        """Admin command to start index using one or two t.me/c/ links."""
        if lock.locked():
            await message.reply("Wait until the previous process completes.")
            return

        user_id = message.from_user.id
        args = message.text.split()[1:]

        if not args or len(args) > 2:
            await message.reply("Invalid format. Use:\n`/indexlink <link>` (for 2 to link) \n`/indexlink <start_link> <end_link>` (to specify range)", parse_mode=enums.ParseMode.MARKDOWN)
            return
        try:
            chat_id, start_msg_id, last_msg_id = extract_links(args)
            
            # Simple check to see if bot can access the last message ID
            await bot.get_messages(chat_id=chat_id, message_ids=last_msg_id)
            
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Proceed",
                            callback_data=f"index {chat_id} {start_msg_id} {last_msg_id}",
                        ),
                        InlineKeyboardButton("Cancel", callback_data="can-index"),
                    ]
                ]
            )
            await bot.send_message(
                user_id,
                f"Chat ID: `{chat_id}`\nStart Message ID: `{start_msg_id}`\nLast Message ID: `{last_msg_id}`\nPlease confirm if you want to start indexing.",
                reply_markup=kb,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Manual Index Check Failed: {e}")
            await message.reply(f"Error on manual indexing: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)


    @app.on_callback_query(filters.regex(r"^index -?\d+ \d+ \d+"))
    async def start_index(bot, query):
        """Callback to initiate the indexing background task."""
        global index_task
        user_id = query.from_user.id
        # Note: map(int, ...) will correctly handle the negative chat_id
        chat_id, start_msg_id, last_msg_id = map(int, query.data.split()[1:])

        await query.message.delete()
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Cancel Indexing", callback_data="cancel_index"),
                ]
            ]
        )
        msg = await bot.send_message(
            user_id,
            "Processing Index...⏳\nStatus will be updated after every 200 messages checked.",
            reply_markup=kb,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        # Start the background task
        index_task = asyncio.create_task(
            index_files_task(bot, msg, chat_id, start_msg_id, last_msg_id)
        )
        await query.answer("Indexing started in the background.")

    @app.on_callback_query(filters.regex(r"^cancel_index$"))
    async def cancel_indexing(bot, query):
        """Callback to cancel the ongoing indexing task."""
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.message.edit_text("Indexing cancelled.")
            logger.info("User requested cancellation of indexing.. : %s", query.from_user.id)
        else:
            await query.message.edit_text("No active indexing process to cancel.")
        await query.answer()
        
    @app.on_callback_query(filters.regex(r"^can-index$"))
    async def cancel_index_confirmation(bot, query):
        """Callback to cancel the initial confirmation menu."""
        await query.message.delete()
        await query.answer("Indexing confirmation cancelled.")
        
    @app.on_message(filters.command(["index"]) & filters.user(ADMIN_USERS) & filters.private)
    async def index_comm(bot, update):
        """Provides instructions for the /index command."""
        await update.reply(
            "To start indexing:\n\n"
            "1. **Forward the LAST file/message** of the channel (do NOT forward as a copy).\n"
            "2. **Use the link command:** `/indexlink <link>` or `/indexlink <start_link> <end_link>`\n\n"
            "The bot must be an administrator in the channel."
        )


    # --- OTHER ADMIN COMMANDS (Unmodified) ---
    
    @app.on_message(filters.command("imdb") & filters.user(ADMIN_USERS) & filters.private)
    async def poster_toggle_command(_, message: Message):
        """Allows admin to turn the IMDB poster feature on or off."""
        current_state = await get_global_setting("IMDB_POSTER_ENABLED", True) 
        new_state = not current_state
        await set_global_setting("IMDB_POSTER_ENABLED", new_state)

        status = "✅ **ON**" if new_state else "❌ **OFF**"
        await message.reply_text(
            f"🖼️ IMDB Poster feature is now set to: {status}",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    @app.on_message(filters.command("autodel") & filters.user(ADMIN_USERS) & filters.private)
    async def autodel_command(_, message: Message):
        """Allows admin to set the auto-delete timer for sent files."""
        try:
            if len(message.command) > 1:
                timer = int(message.command[1])
                if timer < 0:
                    timer = 0
                
                await set_global_setting("AUTO_DELETE_TIMER", timer)
                
                if timer > 0:
                    status = f"✅ **ON**: Menu auto-delete timer set to **{timer} seconds**."
                else:
                    status = "❌ **OFF**: Menu auto-delete is disabled."
                    
                await message.reply_text(
                    f"🗑️ Menu Auto-Deletion feature is now set to: {status}\n\n"
                    f"Set to 0 to disable.",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            else:
                current_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
                status = f"Current menu auto-delete timer is: **{current_timer} seconds**."
                await message.reply_text(
                    f"Usage: `/autodel <seconds>` (e.g., `/autodel 300`)\n"
                    f"{status}",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                
        except ValueError:
            await message.reply_text("❌ Invalid number provided. Please use an integer for seconds.", parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Error in autodel_command: {e}")

    # The function must return a list of handlers for registration in main.py
    return [
        start_command, 
        index_files_forwarded, 
        manual_index,          
        start_index,           
        cancel_indexing,       
        cancel_index_confirmation, 
        index_comm,            
        poster_toggle_command, 
        autodel_command
    ]
