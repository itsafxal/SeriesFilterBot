from pyrogram import enums
from DB.database import get_global_setting
from config import logger


async def check_force_subscribe(cyber, user_id):

    channel = await get_global_setting("FORCE_SUB_CHANNEL_ID", None)
    if not channel:
        return True, None

    channel = int(channel)

    try:
        member = await cyber.get_chat_member(channel, user_id)

        if member.status in [
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER
        ]:
            return True, None

        chat = await cyber.get_chat(channel)
        return False, chat.invite_link

    except Exception as e:
        logger.error(e)
        chat = await cyber.get_chat(channel)
        return False, chat.invite_link
