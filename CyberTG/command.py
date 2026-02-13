import io
import asyncio
import base64
from utils.fsub_helper import check_force_subscribe
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
        START_TEXT = (
            f"🖤 <b>Hey {user_mention}!</b>\n\n"
            f"Welcome to <b>{bot.first_name}</b> — your private cinema in chat 🎭\n"
            "Just type what you’re looking for and I’ll fetch it faster than light ⚡\n\n"
            "<blockquote>Let’s dive into the world of entertainment 🌙</blockquote>"
        )
        CYBER_KEYBOARD = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎞 ʙʀᴏᴡsᴇ ʟɪʙʀᴀʀʏ 🎞",
                    switch_inline_query_current_chat=""
                )
            ],
            [
                InlineKeyboardButton("⚡ ʜᴇʟᴘ", callback_data="help|menu"),
                InlineKeyboardButton("ᴀʙᴏᴜᴛ ✨", callback_data="about|menu")
            ],
            [
                InlineKeyboardButton(
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
        total
    ]
