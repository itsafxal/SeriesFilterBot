import io
import asyncio
import base64

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from config import (
    logger,
    ADMIN_USERS,
    START_IMG,
    LOG_FILE_PATH,
    MAX_LOG_SIZE
)

from DB.database import (
    add_user,
    get_all_users,
    get_total_users,
    get_total_files,
    delete_all_files,
    delete_one_file,
    get_index_channels,
    set_index_channels,
)

from .files import (
    deliver_files_direct,
    set_session
)

from .fsub import (
    check_fsub,
    build_fsub_keyboard,
    fsub_text
)


# =================================================
# REGISTER COMMAND HANDLERS
# =================================================

def register_command_handlers(cyber: Client):

    # =================================================
    # START COMMAND
    # =================================================
    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):

        uid = message.from_user.id
        await add_user(uid)

        # ---------------------------------------------
        # DEEP LINK HANDLER
        # ---------------------------------------------
        if len(message.command) > 1:

            payload = message.command[1]

            # ===============================
            # SEND FILE PAYLOAD
            # ===============================
            if payload.startswith("send_"):

                # FSUB CHECK FIRST
                joined = await check_fsub(cyber, uid)

                if not joined:
                    return await message.reply(
                        fsub_text(),
                        reply_markup=build_fsub_keyboard()
                    )

                try:
                    data = payload.replace("send_", "")
                    data += "=" * (-len(data) % 4)

                    raw = base64.urlsafe_b64decode(data).decode()

                    parts = raw.split("|")
                    if len(parts) != 5:
                        return await message.reply("Invalid link.")

                    owner, series, season, lang, quality = parts

                    if int(owner) != uid:
                        return await message.reply(
                            "⚠️ This link is not for you."
                        )

                    season = int(season)

                except Exception as e:
                    logger.error(f"PAYLOAD ERROR: {e}")
                    return await message.reply("Invalid link.")

                # SAVE SESSION
                set_session(uid, {
                    "series": series,
                    "season": season,
                    "language": lang,
                    "quality": quality
                })

                # START DELIVERY
                await deliver_files_direct(cyber, uid)
                return

        # ---------------------------------------------
        # NORMAL START MESSAGE
        # ---------------------------------------------
        bot = await cyber.get_me()

        await message.reply_photo(
            START_IMG,
            caption=f"Welcome to {bot.first_name}\n\nSend series name."
        )

    # =================================================
    # BROADCAST
    # =================================================
    @cyber.on_message(filters.command("broadcast") & filters.user(ADMIN_USERS))
    async def broadcast(_, msg):

        if not msg.reply_to_message:
            return await msg.reply("Reply to message.")

        users = await get_all_users()

        for u in users:
            try:
                await msg.reply_to_message.copy(u)
                await asyncio.sleep(0.1)
            except:
                pass

        await msg.reply("✅ Broadcast completed.")

    # =================================================
    # TOTAL STATS
    # =================================================
    @cyber.on_message(filters.command("total") & filters.user(ADMIN_USERS))
    async def total(_, m):

        users = await get_total_users()
        files = await get_total_files()

        await m.reply(
            f"👤 Users: {users}\n"
            f"📂 Files: {files}"
        )

    # =================================================
    # DELETE ALL FILES
    # =================================================
    @cyber.on_message(filters.command("deleteall") & filters.user(ADMIN_USERS))
    async def deleteall(_, m):

        await delete_all_files()
        await m.reply("🗑️ All files deleted.")

    # =================================================
    # DELETE SINGLE FILE
    # =================================================
    @cyber.on_message(filters.command("deletefile") & filters.user(ADMIN_USERS))
    async def deletefile(_, m):

        if len(m.command) < 3:
            return await m.reply("Usage:\n/deletefile <cid> <mid>")

        cid = int(m.command[1])
        mid = int(m.command[2])

        await delete_one_file(cid, mid)

        await m.reply("✅ File deleted.")

    # =================================================
    # LOGS (FIXED)
    # =================================================
    @cyber.on_message(filters.command("logs") & filters.user(ADMIN_USERS))
    async def send_logs_cmd(_, message: Message):

        try:
            with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
                log_content = f.read()

            if not log_content:
                return await message.reply_text("🪵 Log file is empty.")

            if len(log_content.encode("utf-8")) > MAX_LOG_SIZE:
                log_content = (
                    f"⚠️ Log truncated.\n\n"
                    + log_content[-(MAX_LOG_SIZE // 2):]
                )

            # IMPORTANT FIX
            log_file = io.BytesIO(
                log_content.encode("utf-8")
            )
            log_file.name = "bot_logs.txt"

            await message.reply_document(
                document=log_file,
                caption="🪵 **Current Bot Logs**",
                parse_mode=enums.ParseMode.MARKDOWN
            )

        except Exception as e:
            logger.error(f"Error sending logs: {e}")
            await message.reply("Failed to send logs.")

    # =================================================
    # ADD INDEX CHANNEL
    # =================================================
    @cyber.on_message(filters.command("addindex") & filters.user(ADMIN_USERS))
    async def add_index_channel(_, message):

        if len(message.command) < 2:
            return await message.reply("Usage:\n/addindex <channel_id>")

        cid = int(message.command[1])

        channels = await get_index_channels()

        if cid in channels:
            return await message.reply("Already exists.")

        channels.append(cid)
        await set_index_channels(channels)

        await message.reply(f"✅ Added: `{cid}`")

    # =================================================
    # DELETE INDEX CHANNEL
    # =================================================
    @cyber.on_message(filters.command("delindex") & filters.user(ADMIN_USERS))
    async def del_index_channel(_, message):

        if len(message.command) < 2:
            return await message.reply("Usage:\n/delindex <channel_id>")

        cid = int(message.command[1])

        channels = await get_index_channels()

        if cid not in channels:
            return await message.reply("Not found.")

        channels.remove(cid)
        await set_index_channels(channels)

        await message.reply(f"🗑️ Removed `{cid}`")

    # =================================================
    # LIST INDEX CHANNELS
    # =================================================
    @cyber.on_message(filters.command("listindex") & filters.user(ADMIN_USERS))
    async def list_index_channels(_, message):

        channels = await get_index_channels()

        if not channels:
            return await message.reply("No index channels.")

        text = "📝 Index Channels:\n\n"

        for cid in channels:
            text += f"• `{cid}`\n"

        await message.reply(
            text,
            parse_mode=enums.ParseMode.MARKDOWN
        )

    logger.info("✅ Command handlers registered.")
