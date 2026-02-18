from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup
from utils.cyber_font import BTN, UI

from config import FORCE_SUB_CHANNELS


async def check_fsub(cyber, user_id):

    not_joined = []

    for ch in FORCE_SUB_CHANNELS:
        try:
            await cyber.get_chat_member(ch, user_id)
        except UserNotParticipant:
            not_joined.append(ch)
        except Exception:
            pass

    return not_joined


def build_fsub_keyboard(channels, payload):

    rows = []

    for ch in channels:
        rows.append([
            BTN("➕ Join Channel", url=f"https://t.me/{str(ch).replace('@','')}")
        ])

    rows.append([
        BTN("✅ Refresh", callback_data=f"fsub_refresh|{payload}")
    ])

    return InlineKeyboardMarkup(rows)
