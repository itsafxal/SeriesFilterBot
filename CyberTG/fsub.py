import asyncio

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup
from pyrogram.errors import ChatAdminRequired, PeerIdInvalid

from config import ADMINS as ADMIN_USERS
from utils.font_helper import BTN, UI
from database import db


# =================================================
# GLOBAL VARIABLES
# =================================================

FSUB_CHANNEL = None
FSUB_LINK = None

# user_id -> (payload, chat_id, msg_id)
WAITING_USERS = {}


# =================================================
# LOAD SETTINGS (CALL ON BOT START)
# =================================================

async def load_fsub_settings():

    global FSUB_CHANNEL, FSUB_LINK

    data = await db.get_global_setting("fsub_channel")

    if data:
        FSUB_CHANNEL = data.get("chat_id")
        FSUB_LINK = data.get("invite_link")


# =================================================
# SAVE SETTINGS
# =================================================

async def save_fsub_settings():

    await db.set_global_setting(
        "fsub_channel",
        {
            "chat_id": FSUB_CHANNEL,
            "invite_link": FSUB_LINK
        }
    )


# =================================================
# CLEAR SETTINGS
# =================================================

async def clear_fsub_settings():

    await db.delete_global_setting("fsub_channel")


# =================================================
# SET FSUB CHANNEL
# =================================================

@Client.on_message(filters.command("sub1") & filters.user(ADMINS))
async def set_sub1(bot, m):

    global FSUB_CHANNEL, FSUB_LINK

    if len(m.command) == 1:
        return await m.reply(
            "Usage:\n`/sub1 -100123456789`",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    raw_id = m.text.split(" ", 1)[1]

    try:
        chat = await bot.get_chat(int(raw_id))

    except ChatAdminRequired:
        return await m.reply("❌ Bot must be admin in channel.")

    except PeerIdInvalid:
        return await m.reply("❌ Bot not in channel.")

    # NORMAL INVITE LINK
    link_obj = await bot.create_chat_invite_link(chat.id)

    FSUB_CHANNEL = chat.id
    FSUB_LINK = link_obj.invite_link

    await save_fsub_settings()

    await m.reply(
        f"✅ Force Subscribe Set\n\n"
        f"🆔 `{chat.id}`\n"
        f"📝 {chat.title}\n"
        f"🔗 [Join Link]({FSUB_LINK})",
        parse_mode=enums.ParseMode.MARKDOWN
    )


# =================================================
# VIEW FSUB STATUS
# =================================================

@Client.on_message(filters.command("view_sub") & filters.user(ADMINS))
async def view_sub(bot, m):

    if not FSUB_CHANNEL:
        return await m.reply("No FSUB channel set.")

    try:
        chat = await bot.get_chat(FSUB_CHANNEL)
        title = chat.title
    except:
        title = "Unknown"

    await m.reply(
        f"📊 FSUB STATUS\n\n"
        f"ID: `{FSUB_CHANNEL}`\n"
        f"Name: {title}\n"
        f"Link: {FSUB_LINK}"
    )


# =================================================
# DELETE FSUB CHANNEL
# =================================================

@Client.on_message(filters.command("delchat") & filters.user(ADMINS))
async def delchat(bot, m):

    global FSUB_CHANNEL, FSUB_LINK

    FSUB_CHANNEL = None
    FSUB_LINK = None

    await clear_fsub_settings()

    await m.reply("✅ Force Subscribe removed.")


# =================================================
# CHECK USER JOINED
# =================================================

async def check_fsub(bot, user_id):

    if not FSUB_CHANNEL:
        return True

    try:
        member = await bot.get_chat_member(
            FSUB_CHANNEL,
            user_id
        )

        if member.status in ["left", "kicked"]:
            return False

    except Exception:
        return False

    return True


# =================================================
# FSUB UI
# =================================================

def build_fsub_keyboard():

    return InlineKeyboardMarkup([
        [BTN("➕ JOIN CHANNEL", url=FSUB_LINK)]
    ])


def fsub_text():

    return UI(
        "⚠️ Join the required channel.\n"
        "Files will start automatically after joining."
    )


# =================================================
# AUTO JOIN DETECT LOOP
# =================================================

async def wait_for_join(bot, user_id):

    # check every 3 seconds (2 min max)
    for _ in range(40):

        joined = await check_fsub(bot, user_id)

        if joined:

            data = WAITING_USERS.pop(user_id, None)

            if not data:
                return

            payload, chat_id, msg_id = data

            # delete fsub message
            try:
                await bot.delete_messages(chat_id, msg_id)
            except:
                pass

            # start delivery automatically
            await bot.send_message(
                user_id,
                f"/start {payload}"
            )

            return

        await asyncio.sleep(3)

    # timeout cleanup
    WAITING_USERS.pop(user_id, None)
