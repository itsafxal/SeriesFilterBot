# command.py - MODIFIED for interactive, link-based indexing, resumption, AND /setskip command

import asyncio
import re
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChannelPrivate
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from config import logger, ADMIN_USERS, INDEX_CHANNELS, START_IMG
from database import (
    add_user,
    get_all_users,
    get_bot_state,
    save_bot_state,
    get_global_setting, 
    set_global_setting,
    delete_all_files, 
    delete_one_file, 
    get_total_files,
    get_total_users,
)
from utils import save_series_file_for_bulk_index 

# --- NEW GLOBALS AND HELPER FUNCTIONS ---
lock = asyncio.Lock()
media_filter = filters.document | filters.video | filters.audio
index_task = None
link_pattern = r"https://t\.me/c/(\d+)/(\d+)" 

# Global key to track if an index is running and where it is
INDEX_STATUS_KEY = "CURRENT_INDEX_TASK" 
# Global key for the message ID to skip up to
INDEX_SKIP_ID_KEY = "INDEX_SKIP_ID" 

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
    """
    The main background task for sequential indexing. 
    start_msg_id is the desired starting point OR the resume checkpoint.
    """
    global index_task
    
    # 1. Check for last processed ID to determine the *true* starting point
    chat_key = f"index_{chat_id}"
    last_processed_id = await get_bot_state(chat_key) 
    
    # Check global skip ID
    skip_id = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
    
    # current starts at the *highest* of: requested start ID, last saved checkpoint, or the global skip ID
    current = max(int(last_processed_id or 0), int(start_msg_id), int(skip_id) + 1)
    
    # If the current start ID is less than the skip ID + 1, we must adjust the message
    if current > int(skip_id) + 1:
        logger.info(f"Indexing start adjusted from base {start_msg_id} to {current} due to resume/skip ID.")


    total_files = 0
    counter = 0
    saved = 0
    total = last_msg_id + 1

    async with lock:
        # 2. Set global status flag
        await set_global_setting(INDEX_STATUS_KEY, f"RUNNING|{chat_id}|{current}|{last_msg_id}")

        try:
            # Inform admin if this is a resumed or skip-adjusted task
            if current > start_msg_id:
                await msg.edit_text(f"Indexing resumed/started for `{chat_id}` from message ID **{current}** (Skipping to {skip_id + 1} if set)...")
            
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
                    await msg.edit_text(f"Indexing process was cancelled at ID **{current}**.")
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
                    # 3. Save current checkpoint and update global status
                    await save_bot_state(chat_key, current)
                    await set_global_setting(INDEX_STATUS_KEY, f"RUNNING|{chat_id}|{current}|{last_msg_id}")
                    
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
            # State already saved on last checkpoint; index_task is set to None in finally
            pass
        except Exception as e:
            # 4. Save final checkpoint before failing
            await save_bot_state(chat_key, current)
            logger.exception(e)
            await msg.edit_text(f"Error while indexing: {e}. Checkpoint saved at ID **{current}**.")
            
        else:
            # 5. Clear checkpoint on successful completion
            await save_bot_state(chat_key, None)
            logger.info("Complete: Total files saved: %s", saved)
            await msg.edit_text(f"🥳 **Complete!** Total **{saved}** Files Saved To Database! Indexing state cleared.")
            
        finally:
            # 6. Clear the global status key
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
            index_task = None

def register_command_handlers(cyber: Client):
    """Registers all command-based message handlers with the Pyrogram client."""

    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):
        """Handles the /start command."""
        await add_user(message.from_user.id)
        user = message.from_user
        bot = await cyber.get_me()
        user_mention = user.mention if user else "User"
        START_TEXT = (
            f"🖤 <b>Hey {user_mention}!</b>\n\n"
            f"Welcome to <b>{bot_info.first_name}</b> — your private cinema in chat 🎭\n"
            "Just type what you’re looking for and I’ll fetch it faster than light ⚡\n\n"
            "<blockquote>Let’s dive into the world of entertainment 🌙</blockquote>"
        )

        # Define the inline keyboard with 'Help' and 'About' buttons
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎞 ʙʀᴏᴡsᴇ ʟɪʙʀᴀʀʏ 🎞", switch_inline_query_current_chat="")
            ],[
                InlineKeyboardButton("⚡ ʜᴇʟᴘ", callback_data="help|menu"),
                InlineKeyboardButton("ᴀʙᴏᴜᴛ ✨", callback_data="about|menu")
            ],[
                InlineKeyboardButton("🍷 sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ 🍷", url="t.me/TomxSupport")
            ]
        ])

        await message.reply_photo(
            photo=START_IMG,
            caption=START_TEXT.format(user_mention, bot),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard
        )

    # --- NEW HANDLER: /setskip ---
    
    @cyber.on_message(filters.command("setskip") & filters.user(ADMIN_USERS) & filters.private)
    async def setskip_command(_, message: Message):
        """Allows admin to set a message ID to skip up to during indexing."""
        try:
            if len(message.command) > 1:
                skip_id = int(message.command[1])
                if skip_id < 0:
                    skip_id = 0

                await set_global_setting(INDEX_SKIP_ID_KEY, skip_id)

                if skip_id > 0:
                    status = f"✅ **ON**: Indexing will skip all messages up to ID **{skip_id}**."
                else:
                    status = "❌ **OFF**: Indexing skip is disabled (Starts from 2 or resume point)."

                await message.reply_text(
                    f"⏭️ Index Skip feature is now set to: {status}\n\n"
                    f"Set to 0 to disable.",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            else:
                current_skip = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
                status = f"Current index skip ID is: **{current_skip}**."
                await message.reply_text(
                    f"Usage: `/setskip <message_id>` (e.g., `/setskip 1000`)\n"
                    f"{status}",
                    parse_mode=enums.ParseMode.MARKDOWN
                )

        except ValueError:
            await message.reply_text("❌ Invalid number provided. Please use an integer for the message ID.", parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Error in setskip_command: {e}")
            await message.reply_text(f"An unexpected error occurred: `{e}`")


    # --- OTHER ADMIN COMMANDS ---

    @cyber.on_message(filters.command("broadcast") & filters.user(ADMIN_USERS))
    async def broadcast_cmd(bot, msg):
        """Broadcasts a replied-to message to all private chats."""
        if not msg.reply_to_message:
            return await msg.reply("reply the message you want to broadcast")

        users = await get_all_users()
        sent = 0
        for uid in users:
            try:  # <-- FIX: Correctly indented to be part of the 'for' loop
                await msg.reply_to_message.copy(uid)
                sent += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"broadcast fail {uid}: {e}")

        await msg.reply(f"✅ broadcast done — sent to **{sent}** users")
        
    @cyber.on_message(filters.command("deleteall") & filters.user(ADMIN_USERS))
    async def deleteall_cmd(_, msg):
        """Deletes all indexed files."""
        await delete_all_files()
        await msg.reply("✅ all files deleted from mongo + sqlite")


    @cyber.on_message(filters.command("deletefile") & filters.user(ADMIN_USERS))
    async def deletefile_cmd(_, msg):
        """Deletes a single indexed file by channel and message ID."""
        # usage: /deletefile <channel_id> <message_id>
        if len(msg.command) < 3:
            return await msg.reply("usage: `/deletefile -10012345 2345`", parse_mode=enums.ParseMode.MARKDOWN)

        try:
            cid = int(msg.command[1])
            mid = int(msg.command[2])
        except ValueError:
             return await msg.reply("❌ Invalid channel ID or message ID. Both must be integers.", parse_mode=enums.ParseMode.MARKDOWN)

        await delete_one_file(cid, mid)
        await msg.reply(f"✅ deleted file **{cid} / {mid}**", parse_mode=enums.ParseMode.MARKDOWN)


    @cyber.on_message(filters.command("total") & filters.user(ADMIN_USERS))
    async def total_cmd(_, msg):
        """Displays the total number of indexed files."""
        total = await get_total_files()
        users = await get_total_users()
        await msg.reply(f"📦 Total Files: **{total}**\n👤 Total Users **{users}**", parse_mode=enums.ParseMode.MARKDOWN)


    # --- HANDLERS FOR INTERACTIVE INDEXING ---

    @cyber.on_message(
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
        chat = origin.chat
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

        # Check for existing checkpoint and global skip ID
        chat_key = f"index_{final_chat_id}"
        resume_id = await get_bot_state(chat_key)
        skip_id = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
        
        # Calculate effective starting ID
        start_id_base = 2
        start_id = max(resume_id or start_id_base, skip_id + 1)
        
        try:
            # Simple check to see if bot can access the last message ID
            await bot.get_messages(chat_id=final_chat_id, message_ids=last_msg_id)

            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"Proceed (Start at ID {start_id})",
                            callback_data=f"index {final_chat_id} {start_id} {last_msg_id}",
                        ),
                        InlineKeyboardButton("Cancel", callback_data="can-index"),
                    ]
                ]
            )
            
            resume_info = ""
            if resume_id and resume_id >= start_id_base:
                 resume_info = f"\n\n⚠️ **A previous index was interrupted. Resuming from ID {resume_id}.**"
            elif start_id > start_id_base:
                 resume_info = f"\n\n⏭️ **Skipping up to ID {skip_id + 1}** (as set by /setskip)."

            await bot.send_message(
                user_id,
                f"Please confirm if you want to start indexing channel `{final_chat_id}` "
                f"from message ID `{start_id}` up to the forwarded message ID `{last_msg_id}`."
                f"{resume_info}",
                reply_markup=kb,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Forward Index Check Failed: {e}")
            await message.reply_text(
                f"Unable to start indexing, either the channel is private and bot is not an admin, or you forwarded the message as copy.\nError: <code>{e}</code>",
                parse_mode=enums.ParseMode.HTML
            )


    @cyber.on_message(
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
            chat_id, start_msg_id_base, last_msg_id = extract_links(args)

            # Check for existing checkpoint and global skip ID
            chat_key = f"index_{chat_id}"
            resume_id = await get_bot_state(chat_key)
            skip_id = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
            
            # Calculate effective starting ID
            start_id = max(resume_id or start_msg_id_base, skip_id + 1)

            # Simple check to see if bot can access the last message ID
            await bot.get_messages(chat_id=chat_id, message_ids=last_msg_id)

            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Proceed",
                            callback_data=f"index {chat_id} {start_id} {last_msg_id}",
                        ),
                        InlineKeyboardButton("Cancel", callback_data="can-index"),
                    ]
                ]
            )
            
            resume_info = ""
            if resume_id and resume_id >= start_msg_id_base:
                 resume_info = f"\n\n⚠️ **A previous index was interrupted. Resuming from ID {resume_id}.**"
            elif start_id > start_msg_id_base:
                 resume_info = f"\n\n⏭️ **Skipping up to ID {skip_id + 1}** (as set by /setskip)."
                 
            await bot.send_message(
                user_id,
                f"Chat ID: `{chat_id}`\nStart Message ID: `{start_id}`\nLast Message ID: `{last_msg_id}`\nPlease confirm if you want to start indexing."
                f"{resume_info}",
                reply_markup=kb,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Manual Index Check Failed: {e}")
            await message.reply(f"Error on manual indexing: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)


    @cyber.on_callback_query(filters.regex(r"^index -?\d+ \d+ \d+"))
    async def start_index(bot, query):
        """Callback to initiate the indexing background task."""
        global index_task
        user_id = query.from_user.id
        
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
            f"Processing Index for `{chat_id}`...⏳\nStatus will be updated after every 200 messages checked. Current ID: **{start_msg_id}**",
            reply_markup=kb,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        # Start the background task
        index_task = asyncio.create_task(
            index_files_task(bot, msg, chat_id, start_msg_id, last_msg_id)
        )
        await query.answer("Indexing started in the background.")

    @cyber.on_callback_query(filters.regex(r"^cancel_index$"))
    async def cancel_indexing(bot, query):
        """Callback to cancel the ongoing indexing task."""
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("Cancelling index...")
            logger.info("User requested cancellation of indexing.. : %s", query.from_user.id)
            # The task will update the message when it finishes cancellation
        else:
            await query.message.edit_text("No active indexing process to cancel.")
            await query.answer("No active index.")

    @cyber.on_callback_query(filters.regex(r"^can-index$"))
    async def cancel_index_confirmation(bot, query):
        """Callback to cancel the initial confirmation menu."""
        await query.message.delete()
        await query.answer("Indexing confirmation cancelled.")

    @cyber.on_message(filters.command(["index"]) & filters.user(ADMIN_USERS) & filters.private)
    async def index_comm(bot, update):
        """Provides instructions for the /index command."""
        await update.reply(
            "To start indexing:\n\n"
            "1. **Forward the LAST file/message** of the channel (do NOT forward as a copy).\n"
            "2. **Use the link command:** `/indexlink <link>` or `/indexlink <start_link> <end_link>`\n\n"
            "The bot must be an administrator in the channel. Use `/setskip` to skip old messages."
        )


    # --- OTHER ADMIN COMMANDS (Unmodified) ---

    @cyber.on_message(filters.command("imdb") & filters.user(ADMIN_USERS) & filters.private)
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

    @cyber.on_message(filters.command("fsub") & filters.user(ADMIN_USERS) & filters.private)
    async def forcesub_command(bot, message: Message):
        """Allows admin to set or disable the force subscribe channel ID."""
        try:
            if len(message.command) > 1:
                channel_id_or_username = message.command[1]

                # Check for "disable" or "off"
                if channel_id_or_username.lower() in ["disable", "off", "0"]:
                    await set_global_setting("FORCE_SUB_CHANNEL_ID", None)
                    status = "❌ **OFF**"
                    await message.reply_text(
                        f"🔒 Force Subscribe feature is now set to: {status}\n\n"
                        f"Users will not be required to join any channel.",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                    return

                # Attempt to resolve the ID/Username to a valid chat ID
                try:
                    chat = await bot.get_chat(channel_id_or_username)
                    final_channel_id = chat.id

                    # Store the ID in the database
                    await set_global_setting("FORCE_SUB_CHANNEL_ID", final_channel_id)

                    status = f"✅ **ON**"
                    await message.reply_text(
                        f"🔒 Force Subscribe feature is now set to: {status}\n"
                        f"Required Channel: `{channel_id_or_username}` (ID: `{final_channel_id}`)\n\n"
                        f"Users must join this channel to get search results.",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                except Exception:
                    await message.reply_text("❌ Invalid channel ID, username, or link provided. Bot must be in the channel.", parse_mode=enums.ParseMode.MARKDOWN)
                    return
            else:
                current_id = await get_global_setting("FORCE_SUB_CHANNEL_ID", None)
                if current_id:
                    # Attempt to fetch the channel info to display the username/title
                    try:
                        chat = await bot.get_chat(current_id)
                        title = chat.title
                        link = chat.invite_link if chat.invite_link else f"tg://resolve?domain={chat.username}"
                        status = f"Current Required Channel: **{title}** (`{current_id}`)\nLink: {link}"
                    except Exception:
                        status = f"Current Required Channel ID: **{current_id}** (Info unavailable)"
                else:
                    status = "Force Subscribe is **disabled**."

                await message.reply_text(
                    f"Usage: `/forcesub <channel_id_or_username_or_link>` (e.g., `/forcesub @MyChannel`)\n"
                    f"To disable: `/forcesub disable`\n\n"
                    f"{status}",
                    parse_mode=enums.ParseMode.MARKDOWN
                )

        except Exception as e:
            logger.error(f"Error in forcesub_command: {e}")
            await message.reply_text(f"An unexpected error occurred: `{e}`")

    @cyber.on_message(filters.command("autodel") & filters.user(ADMIN_USERS) & filters.private)
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
        setskip_command, # NEWLY ADDED
        index_files_forwarded,
        manual_index,
        forcesub_command,
        start_index,
        cancel_indexing,
        cancel_index_confirmation,
        index_comm,
        poster_toggle_command,
        autodel_command,
        broadcast_cmd,
        deleteall_cmd,
        deletefile_cmd,
        total_cmd
    ]
