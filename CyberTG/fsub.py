import asyncio

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup

from utils.cyber_font import BTN, UI
from config import ADMIN_USERS

# IMPORTANT → your real DB helpers
from DB.database import (
    get_global_setting,
    set_global_setting
)


# =================================================
# GLOBAL STATE
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

    try:
        data = await get_global_setting("fsub_channel")

        if data:
            FSUB_CHANNEL = data.get("chat_id")
            FSUB_LINK = data.get("invite_link")

    except Exception as e:
        print("Failed to load fsub settings:", e)


# =================================================
# SAVE SETTINGS
# =================================================

async def save_fsub_settings():

    await set_global_setting(
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

    await set_global_setting("fsub_channel", None)


# =================================================
# ADMIN COMMANDS
# =================================================

def register_fsub_handlers(cyber: Client):

    # ---------------------------------------------
    # SET FSUB CHANNEL
    # ---------------------------------------------
    @cyber.on_message(filters.command("sub1") & filters.user(ADMIN_USERS))
    async def set_sub1(_, m):

        global FSUB_CHANNEL, FSUB_LINK

        if len(m.command) < 2:
            return await m.reply(
                "Usage:\n`/sub1 -100123456789`"
            )

        try:
            cid = int(m.command[1])
            chat = await cyber.get_chat(cid)

        except Exception:
            return await m.reply("❌ Invalid channel ID.")

        # NORMAL invite link (NOT request join)
        link_obj = await cyber.create_chat_invite_link(cid)

        FSUB_CHANNEL = cid
        FSUB_LINK = link_obj.invite_link

        await save_fsub_settings()

        await m.reply(
            f"✅ FSUB Set\n\n"
            f"ID: `{cid}`\n"
            f"Name: {chat.title}\n"
            f"Link: {FSUB_LINK}"
        )

    # ---------------------------------------------
    # VIEW FSUB
    # ---------------------------------------------
    @cyber.on_message(filters.command("view_sub") & filters.user(ADMIN_USERS))
    async def view_sub(_, m):

        if not FSUB_CHANNEL:
            return await m.reply("No FSUB channel set.")

        try:
            chat = await cyber.get_chat(FSUB_CHANNEL)
            title = chat.title
        except:
            title = "Unknown"

        await m.reply(
            f"📊 FSUB STATUS\n\n"
            f"ID: `{FSUB_CHANNEL}`\n"
            f"Name: {title}\n"
            f"Link: {FSUB_LINK}"
        )

    # ---------------------------------------------
    # DELETE FSUB
    # ---------------------------------------------
    @cyber.on_message(filters.command("delchat") & filters.user(ADMIN_USERS))
    async def delchat(_, m):

        global FSUB_CHANNEL, FSUB_LINK

        FSUB_CHANNEL = None
        FSUB_LINK = None

        await clear_fsub_settings()

        await m.reply("✅ FSUB removed.")


# =================================================
# CHECK USER JOINED
# =================================================

async def check_fsub(cyber, user_id):

    if not FSUB_CHANNEL:
        return True

    try:
        member = await cyber.get_chat_member(
            FSUB_CHANNEL,
            user_id
        )

        if member.status in ["left", "kicked"]:
            return False

    except:
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
        "⚠️ Join required channel.\n"
        "Files will start automatically after joining."
    )


# =================================================
# AUTO DETECT JOIN (BIG BOT STYLE)
# =================================================

async def wait_for_join(cyber, user_id):

    # check every 3 seconds (max 2 minutes)
    for _ in range(40):

        joined = await check_fsub(cyber, user_id)

        if joined:

            data = WAITING_USERS.pop(user_id, None)

            if not data:
                return

            payload, chat_id, msg_id = data

            # delete fsub message
            try:
                await cyber.delete_messages(chat_id, msg_id)
            except:
                pass

            # start file delivery automatically
            await cyber.send_message(
                user_id,
                f"/start {payload}"
            )

            return

        await asyncio.sleep(3)

    # cleanup timeout
    WAITING_USERS.pop(user_id, None)
