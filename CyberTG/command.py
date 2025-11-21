# command.py - MODIFIED for interactive, link-based indexing, resumption, AND /setskip command

import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from config import logger, ADMIN_USERS, START_IMG, CUSTOM_CAPTION_TEXT
from DB.database import (
    add_user,
    get_all_users,
    get_global_setting,
    set_global_setting,
    delete_all_files,
    delete_one_file,
    get_total_files,
    get_total_users,
    get_all_files_by_season_and_quality,
    get_index_channels, # Import for channel indexing
    set_index_channels, # Import for channel indexing
)
from .index import register_index_handlers # Import index handlers

# --- GLOBALS REMOVED: lock, media_filter, index_task, link_pattern, INDEX_STATUS_KEY, INDEX_SKIP_ID_KEY, extract_links, index_files_task ---
# The above were moved to index.py

def register_command_handlers(cyber: Client):
    """Registers all command-based message handlers with the Pyrogram client."""

    # Register indexing handlers from the new file
    index_handlers = register_index_handlers(cyber)

    # ================================
    #        ADD INDEX CHANNEL
    # ================================
    @cyber.on_message(filters.command("addindex") & filters.user(ADMIN_USERS))
    async def add_index_channel(_, message):
        if len(message.command) < 2:
            return await message.reply(
                "Usage:\n`/addindex <channel_id>`",
                parse_mode=enums.ParseMode.MARKDOWN
            )

        try:
            cid = int(message.command[1])
        except:
            return await message.reply("❌ Invalid Channel ID. It must be an integer.")

        channels = await get_index_channels()
        if cid in channels:
            return await message.reply("⚠️ Channel ID already exists.")

        channels.append(cid)
        await set_index_channels(channels)

        await message.reply(
            f"✅ Added index channel: `{cid}`",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # ================================
    #        DELETE INDEX CHANNEL
    # ================================
    @cyber.on_message(filters.command("delindex") & filters.user(ADMIN_USERS))
    async def delete_index_channel(_, message):

        if len(message.command) < 2:
            return await message.reply(
                "Usage:\n`/delindex <channel_id>`",
                parse_mode=enums.ParseMode.MARKDOWN
            )

        try:
            cid = int(message.command[1])
        except:
            return await message.reply("❌ Invalid Channel ID. It must be an integer.")

        channels = await get_index_channels()
        if cid not in channels:
            return await message.reply("⚠️ Channel ID not found.")

        channels.remove(cid)
        await set_index_channels(channels)

        text = f"🗑️ Removed index channel: `{cid}`"
        await message.reply(
            text,
            parse_mode=enums.ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

    # ================================
    #        LIST INDEX CHANNELS
    # ================================
    @cyber.on_message(filters.command("listindex") & filters.user(ADMIN_USERS))
    async def list_index_channels(_, message):

        channels = await get_index_channels()
        if not channels:
            return await message.reply("📝 No index channels configured.")

        text = "📝 **Current Index Channels:**\n\n"

        for cid in channels:
            info = f"• ID: `{cid}`\n"
            try:
                chat = await cyber.get_chat(cid)
                info += f"  Title: **{chat.title}**"

                # Export invite link safely
                invite = None
                try:
                    invite = await cyber.export_chat_invite_link(cid)
                except:
                    pass

                if invite:
                    info += f" ([Invite Link]({invite}))"
                elif chat.username:
                    info += f" (@{chat.username})"
                else:
                    info += " (Private/No Link)"

                info += "\n"

            except Exception as e:
                logger.error(f"Error fetching channel {cid}: {e}")
                info += "  Title: `[Access Denied or Not Found]`\n"

            text += info

        await message.reply(
            text,
            parse_mode=enums.ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )


    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):
        """Handles the /start command and deep-link /start send_... payloads."""
        # Ensure user is recorded
        await add_user(message.from_user.id)

        # If there's a start payload like: /start send_<b64>
        start_arg = None
        try:
            if len(message.command) > 1:
                start_arg = message.command[1]
        except Exception:
            start_arg = None

        # Deep-link handling: send_<b64>
        if start_arg and start_arg.startswith("send_"):
            b64 = start_arg[5:]
            # Add padding if missing
            import base64
            try:
                # URL unquote is not strictly required because Telegram passes already decoded,
                # but be defensive: replace spaces and safe chars
                from urllib.parse import unquote
                b64 = unquote(b64)
                # restore padding if necessary
                padding = '=' * (-len(b64) % 4)
                raw = base64.urlsafe_b64decode(b64 + padding).decode("utf-8")
                # raw is series|season|language|quality
                parts = raw.split("|")
                if len(parts) < 4:
                    raise ValueError("Invalid payload")
                series_name = parts[0]
                season = int(parts[1])
                language = parts[2]
                quality = parts[3]
            except Exception as e:
                logger.error(f"[START_DEEPLINK] Failed to decode start payload: {e}")
                return await message.reply_text("❌ Invalid or corrupted start payload.")

            # Check force-subscribe (if configured)
            try:
                from utils import check_force_subscribe
                is_member, invite_link = await check_force_subscribe(cyber, message.from_user.id)
            except Exception as e:
                logger.warning(f"[START_DEEPLINK] force-subscribe check failed: {e}")
                is_member, invite_link = True, None

            if not is_member:
                # Ask user to join via button
                try:
                    await message.reply_text(
                        "🛑 You must join the required channel to receive files. Click below to join and then re-open the bot.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 Join Channel", url=invite_link)]]),
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                except Exception:
                    await message.reply_text("🛑 You must join the required channel to receive files.")
                return

            # If subscribed — fetch files and send them
            await message.reply_text(f"✅ Preparing files for **{series_name}** — Season {season} ({language}) [{quality}]",
                                      parse_mode=enums.ParseMode.MARKDOWN)

            try:
                files = await get_all_files_by_season_and_quality(series_name, season, quality, language)
            except Exception as e:
                logger.error(f"[START_DEEPLINK] DB fetch fail for {series_name} S{season} {language} {quality}: {e}")
                files = []

            if not files:
                return await message.reply_text("❌ No files found for the requested selection.")

            bot_username = (await cyber.get_me()).username
            sent_count = 0
            for file_entry in files:
                channel_id = file_entry["channel_id"]
                message_id = file_entry["message_id"]
                file_name = file_entry.get("file_name", f"S{file_entry.get('season',0)}E{file_entry.get('episode',99)}")
                custom_caption = CUSTOM_CAPTION_TEXT.format(file_name=file_name, bot_username=bot_username)
                try:
                    file_id = file_entry.get("file_id")
                    if file_id:
                        await cyber.send_document(
                            chat_id=message.from_user.id,
                            document=file_id,
                            caption=custom_caption,
                            parse_mode=enums.ParseMode.MARKDOWN,
                        )
                    else:
                        await cyber.copy_message(
                            chat_id=message.from_user.id,
                            from_chat_id=channel_id,
                            message_id=message_id,
                            caption=custom_caption,
                            parse_mode=enums.ParseMode.MARKDOWN,
                        )
                    sent_count += 1
                except Exception as e:
                    logger.error(f"[START_DEEPLINK] Fail sending {message_id} from {channel_id}: {e}")
                    continue

                # throttle
                if sent_count < len(files):
                    await asyncio.sleep(1.5)

            await message.reply_text(f"✅ Delivered {sent_count} files for **{series_name}** — Season {season} ({quality})",
                                     parse_mode=enums.ParseMode.MARKDOWN)
            return

        # --- Default welcome / normal start (unchanged behaviour) ---
        user = message.from_user
        bot = await cyber.get_me()
        user_mention = user.mention if user else "User"
        START_TEXT = (
            f"🖤 <b>Hey {user_mention}!</b>\n\n"
            f"Welcome to <b>{bot.first_name}</b> — your private cinema in chat 🎭\n"
            "Just type what you’re looking for and I’ll fetch it faster than light ⚡\n\n"
            "<blockquote>Let’s dive into the world of entertainment 🌙</blockquote>"
        )

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
    # The new index channel commands are already registered via their decorators inside this function.
    return [
        start_command,
        *index_handlers,
        poster_toggle_command,
        forcesub_command,
        autodel_command,
        broadcast_cmd,
        deleteall_cmd,
        deletefile_cmd,
        total_cmd,
        # The new index channel commands (add_index_channel, delete_index_channel, list_index_channels)
        # are now automatically registered by their @cyber.on_message decorators,
        # but since they are defined inside the function, they must be included in the return list.
        # However, to be consistent with the other commands defined outside the function in the first step,
        # I've included them here. Wait, since they are defined with @cyber.on_message inside
        # this function, they don't need to be in the final return list as they are not
        # being used as a simple function reference, but as decorators on the client object (cyber).
        # Let's keep them in the list for full clarity and consistency with the previous structure.
        add_index_channel,
        delete_index_channel,
        list_index_channels,
    ]
