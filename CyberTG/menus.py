from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    CallbackQuery,
    InputMediaPhoto
)

from config import DEFAULT_POSTER, ADMIN_USERS
from utils.font_helper import BTN, UI

import logging

logger = logging.getLogger(__name__)


# -------------------------------------------------
# HELP MENU
# -------------------------------------------------

async def help_menu_handler(cyber: Client, query: CallbackQuery):

    help_text = (
        f"<b>{UI('💡 Help & Instructions')}</b>\n\n"
        f"{UI('1. Search')}: {UI('Send the movie or series name.')}\n"
        f"{UI('2. Select')}: {UI('Choose Season.')}\n"
        f"{UI('3. Filter')}: {UI('Choose Language and Quality.')}\n"
        f"{UI('4. Receive')}: {UI('Click download button to get files.')}\n\n"
        f"<i>{UI('If no result, try shorter name.')}</i>"
    )

    HELP_KEYBOARD = InlineKeyboardMarkup([
        [
            BTN("🏡 HOME", callback_data="cyber|"),
            BTN("🔅 ADMIN", callback_data="admin|menu")
        ]
    ])

    try:
        await query.message.edit_caption(
            help_text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=HELP_KEYBOARD
        )
    except Exception:
        await query.message.edit_text(
            help_text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=HELP_KEYBOARD
        )

    await query.answer()


# -------------------------------------------------
# HOME MENU
# -------------------------------------------------

async def cyber_menu_handler(cyber: Client, query: CallbackQuery):

    user = query.from_user
    bot = await cyber.get_me()

    START_TEXT = (
        f"🖤 <b>{UI(f'Hey {user.mention}!')}</b>\n\n"
        f"{UI(f'Welcome to {bot.first_name} — your private cinema 🎬')}\n"
        f"{UI('Send series name to begin searching.')}\n\n"
        f"<blockquote>{UI('Let’s dive into entertainment 🌙')}</blockquote>"
    )

    CYBER_KEYBOARD = InlineKeyboardMarkup([
        [BTN("🎞 Browse Library 🎞",
             switch_inline_query_current_chat="")],
        [
            BTN("⚡ Help", callback_data="help|menu"),
            BTN("About ✨", callback_data="about|menu")
        ],
        [BTN("🍷 Support Group 🍷", url="https://t.me/TomxSupport")]
    ])

    try:
        await query.message.edit_media(
            media=InputMediaPhoto(
                media=DEFAULT_POSTER,
                caption=START_TEXT,
                parse_mode=enums.ParseMode.HTML
            ),
            reply_markup=CYBER_KEYBOARD
        )
    except Exception:
        await query.message.edit_text(
            START_TEXT,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=CYBER_KEYBOARD
        )

    await query.answer()


# -------------------------------------------------
# ADMIN MENU
# -------------------------------------------------

async def admin_menu_handler(cyber: Client, query: CallbackQuery):

    user_id = query.from_user.id

    try:
        if isinstance(ADMIN_USERS, (list, tuple, set)):
            allowed = user_id in ADMIN_USERS
        else:
            allowed = user_id == int(ADMIN_USERS)
    except Exception:
        allowed = False

    if not allowed:
        await query.answer(
            UI("Hey, this is not for you 🙂"),
            show_alert=True
        )
        return

    admin_text = (
        f"<b>{UI('ADMIN COMMANDS')}</b>\n\n"
        f"{UI('/broadcast - Broadcast message')}\n"
        f"{UI('/deleteall - Delete all files')}\n"
        f"{UI('/total - Total users/files')}\n"
        f"{UI('/addindex - Add index channel')}\n"
        f"{UI('/delindex - Delete index channel')}"
    )

    ADMIN_KEYBOARD = InlineKeyboardMarkup([
        [
            BTN("🏡 HOME", callback_data="cyber|"),
            BTN("🚫 CLOSE", callback_data="close|menu")
        ]
    ])

    try:
        await query.message.edit_caption(
            admin_text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=ADMIN_KEYBOARD
        )
    except Exception:
        await query.message.edit_text(
            admin_text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=ADMIN_KEYBOARD
        )

    await query.answer()


# -------------------------------------------------
# ABOUT MENU
# -------------------------------------------------

async def about_menu_handler(cyber: Client, query: CallbackQuery):

    about_text = (
        f"<blockquote>{UI('Name: Series Bot')}\n"
        f"{UI('Developer: Cyber TG')}\n"
        f"{UI('Library: Pyrogram')}\n"
        f"{UI('Language: Python 3')}\n"
        f"{UI('Database: MongoDB')}</blockquote>"
    )

    ABOUT_KEYBOARD = InlineKeyboardMarkup([
        [BTN("💻 Source Code",
             url="https://github.com/CyberTG/SeriesFilterBot")],
        [
            BTN("🏡 HOME", callback_data="cyber|"),
            BTN("⚡ HELP", callback_data="help|menu")
        ]
    ])

    try:
        await query.message.edit_caption(
            about_text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=ABOUT_KEYBOARD
        )
    except Exception:
        await query.message.edit_text(
            about_text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=ABOUT_KEYBOARD
        )

    await query.answer()


# -------------------------------------------------
# CLOSE MENU
# -------------------------------------------------

async def close_menu_handler(_, query: CallbackQuery):

    try:
        await query.message.delete()
        await query.answer()
    except Exception:
        await query.answer(UI("Can't close."))


# -------------------------------------------------
# REGISTER CALLBACKS
# -------------------------------------------------

def register_menu_handlers(cyber: Client):

    @cyber.on_callback_query(filters.regex("^help\\|menu$"))
    async def _(c, q):
        await help_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex("^about\\|menu$"))
    async def _(c, q):
        await about_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex("^cyber\\|"))
    async def _(c, q):
        await cyber_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex("^admin\\|menu$"))
    async def _(c, q):
        await admin_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex("^close\\|menu$"))
    async def _(c, q):
        await close_menu_handler(c, q)
