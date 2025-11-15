# command.py - MODIFIED for interactive, link-based indexing, resumption, AND /setskip command

import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from config import logger, ADMIN_USERS, START_IMG
from DB.database import (
    add_user,
    get_all_users,
    get_global_setting,
    set_global_setting,
    delete_all_files,
    delete_one_file,
    get_total_files,
    get_total_users,
)
from .index import register_index_handlers # NEW: Import index handlers

# --- GLOBALS REMOVED: lock, media_filter, index_task, link_pattern, INDEX_STATUS_KEY, INDEX_SKIP_ID_KEY, extract_links, index_files_task ---
# The above were moved to index.py

def register_command_handlers(cyber: Client):
    """Registers all command-based message handlers with the Pyrogram client."""

    # Register indexing handlers from the new file
    index_handlers = register_index_handlers(cyber)

    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):
        """Handles the /start command."""
        await add_user(message.from_user.id)
        text = message.text.strip()
        if text.startswith("/start send_"):
            return  
        user = message.from_user
        bot = await cyber.get_me()
        user_mention = user.mention if user else "User"
        START_TEXT = (
            f"🖤 <b>Hey {user_mention}!</b>\n\n"
            f"Welcome to <b>{bot.first_name}</b> — your private cinema in chat 🎭\n"
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
            caption=START_TEXT,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard
        )

    # --- REMOVED HANDLER: /setskip (Moved to index.py) ---
    # @cyber.on_message(filters.command("setskip") & filters.user(ADMIN_USERS) & filters.private)
    # async def setskip_command... (MOVED)

    # --- OTHER ADMIN COMMANDS ---

    @cyber.on_message(filters.command("broadcast") & filters.user(ADMIN_USERS))
    async def broadcast_cmd(bot, msg):
        """Broadcasts a replied-to message to all private chats."""
        if not msg.reply_to_message:
            return await msg.reply("reply the message you want to broadcast")

        users = await get_all_users()
        sent = 0
        for uid in users:
            try:
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


    # --- HANDLERS FOR INTERACTIVE INDEXING (ALL MOVED TO index.py) ---
    # index_files_forwarded, manual_index, start_index, cancel_indexing, cancel_index_confirmation, index_comm (MOVED)


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
        *index_handlers, # NEW: Include all handlers from index.py
        poster_toggle_command,
        forcesub_command,
        autodel_command,
        broadcast_cmd,
        deleteall_cmd,
        deletefile_cmd,
        total_cmd
    ]
