import io
import asyncio
import base64

from utils import check_force_subscribe
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    logger,
    ADMIN_USERS,
    START_IMG,
    CUSTOM_CAPTION_TEXT,
    LOG_FILE_PATH,
    MAX_LOG_SIZE,
    MAX_CALLBACK_LENGTH,
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

from .index import register_index_handlers
from .session_store import set_session
from .delivery import deliver_files_direct


# -------------------------------------------------
# REGISTER
# -------------------------------------------------
def register_command_handlers(cyber: Client):

    index_handlers = register_index_handlers(cyber)

    # =================================================
    # START + DEEP LINK RECEIVER
    # =================================================
    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):

        user_id = message.from_user.id
        await add_user(user_id)

        # ---------------- FORCE SUB ----------------
        try:
            is_member, invite_link = await check_force_subscribe(cyber, user_id)
        except Exception:
            is_member, invite_link = True, None

        if not is_member:
            return await message.reply_text(
                "🛑 **You must join our channel to use this bot.**\n\nAfter joining, press Start again.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📣 Join Channel", url=invite_link)]
                ]),
                parse_mode=enums.ParseMode.MARKDOWN
            )

        # ---------------- DEEP LINK PAYLOAD ----------------
        if len(message.command) > 1:
            payload = message.command[1]

            if payload.startswith("send_"):
                try:
                    raw = base64.urlsafe_b64decode(payload.replace("send_", "")).decode()
                    owner, series, season, lang, quality = raw.split("|")
                    season = int(season)
                except Exception:
                    return await message.reply("❌ Invalid link")

                set_session(user_id, {
                    "series": series,
                    "season": season,
                    "language": lang,
                    "quality": quality
                })

                await message.reply(
                    f"📥 Sending files…\n\n{series}\nSeason {season}\n{lang} | {quality}"
                )

                await deliver_files_direct(cyber, user_id)
                return

        # ---------------- NORMAL START ----------------
        bot = await cyber.get_me()
        user = message.from_user

        START_TEXT = (
            f"🖤 <b>Hey {user.mention}!</b>\n\n"
            f"Welcome to <b>{bot.first_name}</b> 🎭\n"
            "Just type what you’re looking for ⚡\n\n"
            "<blockquote>Your private cinema awaits 🌙</blockquote>"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎞 Browse Library", switch_inline_query_current_chat="")],
            [
                InlineKeyboardButton("⚡ Help", callback_data="help|menu"),
                InlineKeyboardButton("✨ About", callback_data="about|menu"),
            ],
            [InlineKeyboardButton("🍷 Support Group", url="t.me/TomxSupport")]
        ])

        await message.reply_photo(
            photo=START_IMG,
            caption=START_TEXT,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard
        )

    # =================================================
    # LOGS
    # =================================================
    @cyber.on_message(filters.command("logs") & filters.user(ADMIN_USERS))
    async def send_logs_cmd(_, message: Message):

        try:
            with open(LOG_FILE_PATH, "r") as f:
                content = f.read()

            if not content:
                return await message.reply_text("Log file empty.")

            if len(content.encode()) > MAX_LOG_SIZE:
                content = content[-(MAX_LOG_SIZE // 2):]

            file = io.StringIO(content)
            file.name = "bot_logs.txt"

            await message.reply_document(file, caption="Bot Logs")

        except Exception as e:
            await message.reply_text(str(e))

    # =================================================
    # INDEX CHANNEL MANAGEMENT
    # =================================================
    @cyber.on_message(filters.command("addindex") & filters.user(ADMIN_USERS))
    async def add_index(_, m):

        if len(m.command) < 2:
            return await m.reply("Usage: /addindex <channel_id>")

        cid = int(m.command[1])
        chans = await get_index_channels()

        if cid in chans:
            return await m.reply("Already exists")

        chans.append(cid)
        await set_index_channels(chans)
        await m.reply(f"Added `{cid}`")

    @cyber.on_message(filters.command("delindex") & filters.user(ADMIN_USERS))
    async def del_index(_, m):

        cid = int(m.command[1])
        chans = await get_index_channels()

        if cid not in chans:
            return await m.reply("Not found")

        chans.remove(cid)
        await set_index_channels(chans)
        await m.reply(f"Removed `{cid}`")

    @cyber.on_message(filters.command("listindex") & filters.user(ADMIN_USERS))
    async def list_index(_, m):

        chans = await get_index_channels()
        if not chans:
            return await m.reply("No channels")

        txt = "Indexed Channels:\n\n"
        for c in chans:
            txt += f"• `{c}`\n"

        await m.reply(txt)

    # =================================================
    # BROADCAST
    # =================================================
    @cyber.on_message(filters.command("broadcast") & filters.user(ADMIN_USERS))
    async def broadcast(_, msg):

        if not msg.reply_to_message:
            return await msg.reply("Reply to a message")

        users = await get_all_users()
        sent = 0

        for u in users:
            try:
                await msg.reply_to_message.copy(u)
                sent += 1
                await asyncio.sleep(0.1)
            except:
                pass

        await msg.reply(f"Broadcast sent to {sent} users")

    # =================================================
    # FILE ADMIN
    # =================================================
    @cyber.on_message(filters.command("deleteall") & filters.user(ADMIN_USERS))
    async def deleteall(_, m):
        await delete_all_files()
        await m.reply("All files deleted")

    @cyber.on_message(filters.command("deletefile") & filters.user(ADMIN_USERS))
    async def deletefile(_, m):

        cid = int(m.command[1])
        mid = int(m.command[2])

        await delete_one_file(cid, mid)
        await m.reply("Deleted")

    @cyber.on_message(filters.command("total") & filters.user(ADMIN_USERS))
    async def total(_, m):

        files = await get_total_files()
        users = await get_total_users()

        await m.reply(f"Files: {files}\nUsers: {users}")

    # =================================================
    # SETTINGS
    # =================================================
    @cyber.on_message(filters.command("imdb") & filters.user(ADMIN_USERS))
    async def imdb_toggle(_, m):

        cur = await get_global_setting("IMDB_POSTER_ENABLED", True)
        await set_global_setting("IMDB_POSTER_ENABLED", not cur)
        await m.reply(f"IMDB Poster = {not cur}")

    @cyber.on_message(filters.command("autodel") & filters.user(ADMIN_USERS))
    async def autodel(_, m):

        t = int(m.command[1])
        await set_global_setting("AUTO_DELETE_TIMER", t)
        await m.reply(f"Auto delete set to {t}")

    # =================================================
    return [
        start_command,
        *index_handlers,
        send_logs_cmd,
        add_index,
        del_index,
        list_index,
        broadcast,
        deleteall,
        deletefile,
        total,
        imdb_toggle,
        autodel,
    ]
