import io
import asyncio
import base64
from utils.cyber_filter import decode_payload
from utils.cyber_font import BTN, UI, small_caps
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
)
from pyrogram.types import (
    InlineKeyboardMarkup,
    InputMediaPhoto
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
    set_global_setting, 
    get_global_setting,
)

from .files import (
    deliver_files_direct,
    set_session
)

from .fsub import (
    check_fsub,
    fsub_text,
    build_fsub_keyboard,
    save_pending
)


# =================================================
# REGISTER COMMAND HANDLERS
# =================================================

def register_command_handlers(cyber: Client):

    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):

        uid = message.from_user.id
        await add_user(uid)

        # ---------------------------------------------
        # DEEP LINK HANDLER
        # ---------------------------------------------
        if len(message.command) > 1:

            payload = message.command[1]

            # =========================================
            # SEND PAYLOAD
            # =========================================
            if payload.startswith("send_"):

                # ------------------------------
                # FSUB CHECK FIRST
                # ------------------------------
                joined = await check_fsub(cyber, uid)

                if not joined:

                    bot = await cyber.get_me()

                    # save pending payload
                    save_pending(uid, payload)

                    # Send FSUB message
                    await message.reply(
                        fsub_text(),
                        reply_markup=build_fsub_keyboard(
                            bot.username,
                            payload
                        )
                    )

                    # 🔥 Delete /start payload message
                    try:
                        await message.delete()
                    except:
                        pass

                    return

                # ------------------------------
                # PAYLOAD DECODE
                # ------------------------------
                try:
                    decoded = decode_payload(payload)
                    if not decoded:
                        await message.reply("Invalid or expired link.")
                        await message.delete()
                        return

                    owner, series, season, lang, quality = decoded

                    if int(owner) != uid:
                        await message.reply("⚠️ This link is not for you.")
                        await message.delete()
                        return

                    season = int(season)

                except Exception as e:
                    logger.error(f"PAYLOAD ERROR: {e}")
                    await message.reply("Invalid link.")
                    await message.delete()
                    return

                # ------------------------------
                # START FILE DELIVERY
                # ------------------------------
                set_session(uid, {
                    "series": series,
                    "season": season,
                    "language": lang,
                    "quality": quality
                })

                await deliver_files_direct(cyber, uid)

                # 🔥 Delete /start payload message
                try:
                    await message.delete()
                except:
                    pass

                return

        # ---------------------------------------------
        # NORMAL START MESSAGE
        # ---------------------------------------------
        # NORMAL START
        bot = await cyber.get_me()
        user = message.from_user
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
                BTN("🎞 ʙʀᴏᴡsᴇ ʟɪʙʀᴀʀʏ 🎞",
                    switch_inline_query_current_chat="")
            ],
            [
                BTN("⚡ ʜᴇʟᴘ", callback_data="help|menu"),
                BTN("ᴀʙᴏᴜᴛ ✨", callback_data="about|menu")
            ],
            [
                BTN("🍷 sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ 🍷",
                    url="https://t.me/TomxSupport")
            ]
        ])

        await message.reply_photo(
            START_IMG,
            caption=START_TEXT,
            reply_markup=CYBER_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
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
    # DELETE ONE FILE
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
    # INDEX CHANNEL MANAGEMENT
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

        await message.reply(f"✅ Added `{cid}`")

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

    @cyber.on_message(filters.command("listindex") & filters.user(ADMIN_USERS))
    async def list_index_channels(_, message):

        channels = await get_index_channels()

        if not channels:
            return await message.reply("No index channels.")

        text = "📝 Index Channels:\n\n"
        text += "\n".join([f"• `{c}`" for c in channels])

        await message.reply(
            text,
            parse_mode=enums.ParseMode.MARKDOWN
        )

    logger.info("✅ Command handlers registered.")

    @cyber.on_message(filters.command("autodel") & filters.user(ADMIN_USERS))
    async def set_autodel_command(_, message: Message):

        KEY = "AUTO_DELETE_DELAY"

        # Show current status
        if len(message.command) < 2:
            current = await get_global_setting(KEY, 0)
            status = "OFF" if current == 0 else f"{current} seconds"
            return await message.reply(
                f"Usage:\n"
                f"/autodel <seconds>\n\n"
                f"Current auto-delete: {status}"
            )

        value = message.command[1].lower()

        # Turn OFF
        if value in ["off", "0"]:
            await set_global_setting(KEY, 0)
            return await message.reply("🛑 Auto-delete disabled.")

        # Set seconds
        try:
            seconds = int(value)

            if seconds < 0:
                return await message.reply("Seconds must be positive.")

            await set_global_setting(KEY, seconds)

            await message.reply(
                f"⏳ Auto-delete set to {seconds} seconds."
            )

        except ValueError:
            await message.reply(
                "Invalid value.\n\nExample:\n/autodel 300\n/autodel off"
            )

    @cyber.on_message(filters.command("filedel") & filters.user(ADMIN_USERS))
    async def set_autodel_command(_, message: Message):

        KEY = "FILE_AUTODEL_DELAY"

        # Show current status
        if len(message.command) < 2:
            current = await get_global_setting(KEY, 0)
            status = "OFF" if current == 0 else f"{current} seconds"
            return await message.reply(
                f"Usage:\n"
                f"/filedel <seconds>\n\n"
                f"Current file-auto-delete: {status}"
            )

        value = message.command[1].lower()

        # Turn OFF
        if value in ["off", "0"]:
            await set_global_setting(KEY, 0)
            return await message.reply("🛑 Auto-delete disabled.")

        # Set seconds
        try:
            seconds = int(value)

            if seconds < 0:
                return await message.reply("Seconds must be positive.")

            await set_global_setting(KEY, seconds)

            await message.reply(
                f"⏳ File Auto-delete set to {seconds} seconds."
            )

        except ValueError:
            await message.reply(
                "Invalid value.\n\nExample:\n/filedel 300\n/filedel off"
            )
            

    # =================================================
    # SET FILE CAPTION (Supports MarkdownV2)
    # =================================================
    @cyber.on_message(filters.command("setcaption") & filters.user(ADMIN_USERS))
    async def set_file_caption(_, message: Message):

        KEY = "CUSTOM_FILE_CAPTION"

        # Show current caption if no argument
        if len(message.command) < 2 and not message.reply_to_message:
            current = await get_global_setting(KEY, None)

            if not current:
                return await message.reply(
                    "No custom caption set.\n\n"
                    "Usage:\n"
                    "/setcaption Your caption here\n\n"
                    "Available variables:\n"
                    "{series}\n"
                    "{file_name}\n"
                    "{season}\n"
                    "{episode}\n"
                    "{language}\n"
                    "{quality}"
                )

            return await message.reply(
                f"📄 Current Caption:\n\n{current}",
                parse_mode=enums.ParseMode.MARKDOWN_V2
            )

        # Get caption from reply OR inline text
        if message.reply_to_message:
            new_caption = message.reply_to_message.text
        else:
            try:
                new_caption = message.text.split(None, 1)[1]
            except IndexError:
                return await message.reply("Caption cannot be empty.")

        if not new_caption.strip():
            return await message.reply("Caption cannot be empty.")

        # Save caption
        await set_global_setting(KEY, new_caption)

        await message.reply(
            "✅ File caption updated successfully.\n\n"
            "Use /setcaption to view it.",
            parse_mode="markdown"
        )


    # =================================================
    # RESET FILE CAPTION
    # =================================================
    @cyber.on_message(filters.command("resetcaption") & filters.user(ADMIN_USERS))
    async def reset_file_caption(_, message: Message):

        await set_global_setting("CUSTOM_FILE_CAPTION", None)

        await message.reply(
            "♻️ Caption reset to default",
            parse_mode="markdown"
        )
