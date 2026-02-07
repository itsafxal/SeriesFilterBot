from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from DB.groupsetting import set_group


def build_group_menu(cfg):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Strict: {cfg['strict']}", callback_data="gs|strict")],
        [InlineKeyboardButton(f"Poster: {cfg['poster']}", callback_data="gs|poster")],
        [InlineKeyboardButton(f"IMDB: {cfg['imdb']}", callback_data="gs|imdb")],
        [InlineKeyboardButton(f"Delete Sug: {cfg['delete_suggestions']}", callback_data="gs|delete_suggestions")],
    ])


async def handle_group_toggle(_, q):
    _, key = q.data.split("|")
    chat = q.message.chat.id

    cfg = await get_group(chat)
    val = not cfg[key]

    await set_group(chat, key, val)

    new = await get_group(chat)
    await q.message.edit_reply_markup(build_group_menu(new))
    await q.answer()
