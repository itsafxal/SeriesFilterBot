import io
import asyncio
import base64
from utils.cyber_fsub import check_force_subscribe
from utils.cyber_font import BTN, UI
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from .index import register_index_handlers
from .files import deliver_files_direct, set_session
from config import (
    logger,
    ADMIN_USERS,
    START_IMG,
    LOG_FILE_PATH,
    MAX_LOG_SIZE,
)

from DB.database import (
    add_user,
    get_all_users,
    get_global_setting,
    set_global_setting,
    delete_all_files,
    delete_one_file,
    get_total_files,
    get_total_users,
    get_index_channels,
    set_index_channels,
)


# =================================================
def register_command_handlers(cyber: Client):

    index_handlers = register_index_handlers(cyber)

    # =================================================
    # START + DEEP LINK
    # =================================================
    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):

        uid = message.from_user.id
        await add_user(uid)

        # FORCE SUB
        is_member, invite = await check_force_subscribe(cyber, uid)
        if not is_member:
            return await message.reply(
                "Join channel first",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Join", url=invite)]
                ])
            )

        # -------- DEEP LINK ----------
        if len(message.command) > 1:
            payload = message.command[1]

            if payload.startswith("send_"):
                try:
                    data = payload.replace("send_", "")
                    # restore base64 padding
                    data += "=" * (-len(data) % 4)

                    raw = base64.urlsafe_b64decode(data).decode()

                    owner, series, season, lang, quality = raw.split("|")

                    # lock to original user
                    if int(owner) != uid:
                        return await message.reply("⚠️ This link is not for you.")

                    season = int(season)

                except Exception as e:
                    logger.error(f"PAYLOAD ERROR: {e}")
                    return await message.reply("Invalid link")

                # store session
                set_session(uid, {
                    "series": series,
                    "season": season,
                    "language": lang,
                    "quality": quality
                })

                # start delivery (progress handled by send_queue)
                await deliver_files_direct(cyber, uid)
                return

        # NORMAL START
        bot = await cyber.get_me()
        user = message.from_user
        user_mention = user.mention
        user_name = user.first_name
        START_TEXT = (
            f"<b>{UI(f'Hey {user_name}!')}</b>\n\n"
            f"<b>{UI(f'Welcome to {bot.first_name}')}</b>\n\n"
            f"{UI('Your private cinema inside Telegram 🎬')}\n"
            f"{UI('Send any series name to begin.')}\n\n"
            f"<blockquote>{UI('Let’s dive into entertainment 🌙')}</blockquote>"
        )
        CYBER_KEYBOARD = InlineKeyboardMarkup([
            [
                BTN(
                    "🎞 ʙʀᴏᴡsᴇ ʟɪʙʀᴀʀʏ 🎞",
                    switch_inline_query_current_chat=""
                )
            ],
            [
                BTN("⚡ ʜᴇʟᴘ", callback_data="help|menu"),
                BTN("ᴀʙᴏᴜᴛ ✨", callback_data="about|menu")
            ],
            [
                BTN(
                    "🍷 sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ 🍷",
                    url="https://t.me/TomxSupport"
                )
            ]
        ])

        await message.reply_photo(
            START_IMG,
            caption=START_TEXT,
            reply_markup=CYBER_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
        )

    
    # =================================================
    @cyber.on_message(filters.command("broadcast") & filters.user(ADMIN_USERS))
    async def broadcast(_, msg):

        if not msg.reply_to_message:
            return await msg.reply("reply message")

        users = await get_all_users()
        for u in users:
            try:
                await msg.reply_to_message.copy(u)
                await asyncio.sleep(0.1)
            except:
                pass

        await msg.reply("Broadcast done")

    @cyber.on_message(filters.command("logs") & filters.user(ADMIN_USERS))
    async def send_logs_cmd(_, message: Message):
        """Sends the bot's log file to the admin."""
        try:
            with open(LOG_FILE_PATH, 'r') as f:
                log_content = f.read()

            if not log_content:
                return await message.reply_text("🪵 Log file is currently empty.")

            # Check size before sending
            # MAX_LOG_SIZE is now imported from config
            if len(log_content.encode('utf-8')) > MAX_LOG_SIZE:
                log_content = f"⚠️ Log file truncated. Showing last {MAX_LOG_SIZE // 1024} KB.\n\n" + log_content[-(MAX_LOG_SIZE // 2):]

            # Use io.StringIO to create a file-like object in memory
            log_file = io.StringIO(log_content)
            log_file.name = "bot_logs.txt"

            await message.reply_document(
                document=log_file,
                caption="🪵 **Current Bot Logs**",
                parse_mode=enums.ParseMode.MARKDOWN
            )

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

    @cyber.on_message(filters.command("deleteall") & filters.user(ADMIN_USERS))
    async def deleteall(_, m):
        await delete_all_files()
        await m.reply("All deleted")

    @cyber.on_message(filters.command("deletefile") & filters.user(ADMIN_USERS))
    async def deletefile(_, m):

        cid = int(m.command[1])
        mid = int(m.command[2])

        await delete_one_file(cid, mid)
        await m.reply("Deleted")

    @cyber.on_message(filters.command("total") & filters.user(ADMIN_USERS))
    async def total(_, m):

        f = await get_total_files()
        u = await get_total_users()

        await m.reply(f"Files: {f}\nUsers: {u}")

    return [
        start_command,
        *index_handlers,
        broadcast,
        deleteall,
        deletefile,
        total,
        send_logs_cmd,
        add_index_channel,
        delete_index_channel,
        list_index_channels,
    ]
