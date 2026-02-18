import asyncio

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    ChatJoinRequest
)

from utils.cyber_font import BTN, UI
from DB.database import (
    get_global_setting,
    set_global_setting
)

# =================================================
# GLOBAL STATE
# =================================================

FSUB_CHANNEL = None
FSUB_LINK = None

# user_id -> payload
PENDING_USERS = {}


# =================================================
# LOAD SETTINGS
# =================================================

async def load_fsub_settings():

    global FSUB_CHANNEL, FSUB_LINK

    data = await get_global_setting("fsub_channel")

    if data:
        FSUB_CHANNEL = data.get("chat_id")
        FSUB_LINK = data.get("invite_link")


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
# FSUB TEXT
# =================================================

def fsub_text():

    return UI(
        "⚠️ You must join required channel.\n"
        "Send join request — files will start automatically."
    )


# =================================================
# FSUB KEYBOARD
# =================================================

def build_fsub_keyboard(bot_username, payload):

    return InlineKeyboardMarkup([
        [
            BTN("➕ REQUEST TO JOIN", url=FSUB_LINK)
        ],
        [
            BTN(
                "☘️ RETRY DOWNLOAD ☘️",
                url=f"https://t.me/{bot_username}?start={payload}"
            )
        ]
    ])


# =================================================
# SAVE PENDING USER
# =================================================

def save_pending(user_id, payload):
    PENDING_USERS[user_id] = payload


def pop_pending(user_id):
    return PENDING_USERS.pop(user_id, None)


# =================================================
# AUTO APPROVE JOIN REQUEST
# =================================================

@Client.on_chat_join_request()
async def auto_approve(bot, join_req: ChatJoinRequest):

    global FSUB_CHANNEL

    try:

        if join_req.chat.id != FSUB_CHANNEL:
            return

        uid = join_req.from_user.id

        # AUTO APPROVE
        await join_req.approve()

        # check pending payload
        payload = pop_pending(uid)

        if payload:
            await bot.send_message(
                uid,
                f"/start {payload}"
            )

    except Exception as e:
        print("JOIN APPROVE ERROR:", e)
