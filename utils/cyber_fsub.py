from pyrogram.types import InlineKeyboardMarkup
from utils.font_helper import BTN, UI
import temp


PENDING_FSUB = {}

async def check_fsub(bot, user_id):

    if not temp.REQ_CHANNEL1:
        return True  # no fsub set

    try:
        member = await bot.get_chat_member(
            temp.REQ_CHANNEL1,
            user_id
        )

        if member.status in ["left", "kicked"]:
            return False

    except Exception:
        return False

    return True


def save_pending(user_id, payload):
    PENDING_FSUB[user_id] = payload


def get_pending(user_id):
    return PENDING_FSUB.get(user_id)


def pop_pending(user_id):
    return PENDING_FSUB.pop(user_id, None)


def build_fsub_keyboard(bot):

    return InlineKeyboardMarkup([
        [BTN("➕ REQUEST TO JOIN", url=bot.req_link1)]
    ])


def fsub_text():

    return UI(
        "⚠️ You must join required channel.\n"
        "After approval files will start automatically."
    )
