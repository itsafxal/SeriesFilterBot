from pyrogram import Client, filters, enums
from pyrogram.errors import ChatAdminRequired, PeerIdInvalid
from pyrogram.types import (
    ChatJoinRequest,
    InlineKeyboardMarkup
)

from config import ADMINS
from utils.font_helper import BTN, UI


# =================================================
# RUNTIME STORAGE (NO temp.py NEEDED)
# =================================================

# channel + invite link
FSUB_CHANNEL = None
FSUB_LINK = None

# pending users waiting for join
# user_id -> payload
PENDING_USERS = {}


# =================================================
# ADMIN COMMAND → SET FSUB
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
        return await m.reply("❌ Bot not inside this channel.")

    # create JOIN REQUEST invite
    link_obj = await bot.create_chat_invite_link(
        chat.id,
        creates_join_request=True
    )

    FSUB_CHANNEL = chat.id
    FSUB_LINK = link_obj.invite_link

    await m.reply(
        f"✅ Force Subscribe Set\n\n"
        f"🆔 ID: `{chat.id}`\n"
        f"📝 Name: {chat.title}\n"
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

    text = (
        f"📊 Force Subscribe Status\n\n"
        f"ID: `{FSUB_CHANNEL}`\n"
        f"Name: {title}\n"
        f"Link: {FSUB_LINK}"
    )

    await m.reply(text)


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
# BUILD FSUB UI
# =================================================

def build_fsub_keyboard():

    return InlineKeyboardMarkup([
        [BTN("➕ REQUEST TO JOIN", url=FSUB_LINK)]
    ])


def fsub_text():

    return UI(
        "⚠️ You must join required channel.\n"
        "After approval files will start automatically."
    )


# =================================================
# SAVE USER WAITING FOR FSUB
# =================================================

def save_pending(user_id, payload):
    PENDING_USERS[user_id] = payload


def pop_pending(user_id):
    return PENDING_USERS.pop(user_id, None)


# =================================================
# AUTO APPROVE JOIN REQUEST
# =================================================

@Client.on_chat_join_request()
async def join_request(bot, join_req: ChatJoinRequest):

    global FSUB_CHANNEL

    try:

        if join_req.chat.id != FSUB_CHANNEL:
            return

        user_id = join_req.from_user.id

        # approve instantly
        await join_req.approve()

        # check pending download
        payload = pop_pending(user_id)

        if payload:
            # AUTO START FILE DELIVERY
            await bot.send_message(
                user_id,
                f"/start {payload}"
            )

    except Exception as e:
        print("JOIN REQUEST ERROR:", e)
