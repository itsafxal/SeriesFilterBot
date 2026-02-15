import asyncio
import shelve
from pyrogram import Client, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, CallbackQuery
from pyrogram.errors import MessageNotModified
from utils.font_helper import BTN, UI

# -------------------------------------------------
# HELP MENU
# -------------------------------------------------

async def help_menu_handler(cyber: Client, query: CallbackQuery):

    help_text = (
        f"<b>{UI('💡 Help & Instructions')}</b>\n\n"
        f"{UI('1. Search')}: {UI('Simply send the name of the movie or series you are looking for.')}\n"
        f"{UI('2. Select')}: {UI('Tap the Season button for a series.')}\n"
        f"{UI('3. Filter')}: {UI('Choose the Language and then the Quality.')}\n"
        f"{UI('4. Receive')}: {UI('The bot will automatically start sending the files to you.')}\n\n"
        f"<i>{UI('If you see No series found, try a slightly different or shorter name.')}</i>"
    )

    HELP_KEYBOARD = InlineKeyboardMarkup([
        [
            BTN("🏡 HOME", callback_data="cyber|"),
            BTN("🔅 ADMIN (ONLY)", callback_data="admin_cmd|")
        ]
    ])

    try:
        await query.message.edit_caption(
            help_text,
            reply_markup=HELP_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
        )
    except Exception:
        await query.message.edit_text(
            help_text,
            reply_markup=HELP_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
        )

    await query.answer(UI("Showing help menu."))


# -------------------------------------------------
# HOME MENU
# -------------------------------------------------

async def cyber_menu_handler(cyber: Client, query: CallbackQuery):

    user = query.from_user
    bot = await cyber.get_me()

    START_TEXT = (
        f"🖤 <b>{UI(f'Hey {user.mention}!')}</b>\n\n"
        f"{UI(f'Welcome to {bot.first_name} — your private cinema in chat 🎭')}\n"
        f"{UI('Just type what you are looking for and I will fetch it fast ⚡')}\n\n"
        f"<blockquote>{UI('Let’s dive into the world of entertainment 🌙')}</blockquote>"
    )

    CYBER_KEYBOARD = InlineKeyboardMarkup([
        [BTN("🎞 Browse Library 🎞", switch_inline_query_current_chat="")],
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
            reply_markup=CYBER_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
        )

    await query.answer(UI("Back To Home"))


# -------------------------------------------------
# ADMIN MENU
# -------------------------------------------------

async def admin_menu_handler(cyber: Client, query: CallbackQuery):

    user_id = query.from_user.id

    if isinstance(ADMIN_USERS, (list, tuple, set)):
        authorized = user_id in ADMIN_USERS
    else:
        authorized = user_id == int(ADMIN_USERS)

    if not authorized:
        await query.answer(
            UI("You are not authorized."),
            show_alert=True
        )
        return

    admin_text = (
        f"<b>{UI('ADMIN COMMANDS')}</b>\n\n"
        f"{UI('/setskip - Skip indexing range')}\n"
        f"{UI('/broadcast - Broadcast message')}\n"
        f"{UI('/deleteall - Delete all files')}\n"
        f"{UI('/deletefile - Delete replied file')}\n"
        f"{UI('/total - Total users/files')}\n"
        f"{UI('/imdb - Toggle imdb')}\n"
        f"{UI('/fsub - Force subscribe')}\n"
        f"{UI('/autodel - Auto delete time')}\n"
        f"{UI('/addindex - Add index channel')}\n"
        f"{UI('/delindex - Delete index channel')}\n"
        f"{UI('/listindex - List channels')}"
    )

    ADMIN_KEYBOARD = InlineKeyboardMarkup([
        [
            BTN("🏡 HOME", callback_data="cyber|"),
            BTN("🚫 CLOSE", callback_data="close")
        ]
    ])

    try:
        await query.message.edit_caption(
            admin_text,
            reply_markup=ADMIN_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
        )
    except Exception:
        await query.message.edit_text(
            admin_text,
            reply_markup=ADMIN_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
        )

    await query.answer(UI("Showing admin info."))


# -------------------------------------------------
# ABOUT MENU
# -------------------------------------------------

async def about_menu_handler(cyber: Client, query: CallbackQuery):

    about_text = (
        f"<blockquote>{UI('My Name: Series Bot')}\n"
        f"{UI('Creator: This Person')}\n"
        f"{UI('Developer: Cyber TG')}\n"
        f"{UI('Library: Pyrogram')}\n"
        f"{UI('Language: Python 3')}\n"
        f"{UI('Primary DB: MongoDB')}\n"
        f"{UI('Secondary DB: SQL DB')}</blockquote>"
    )

    ABOUT_KEYBOARD = InlineKeyboardMarkup([
        [BTN("💻 Source Code",
             url="https://github.com/CyberTG/SeriesFilterBot")],
        [
            BTN("🏡 HOME", callback_data="cyber|home"),
            BTN("🕵‍♂ HELP", callback_data="help|menu")
        ]
    ])

    try:
        await query.message.edit_caption(
            about_text,
            reply_markup=ABOUT_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
        )
    except Exception:
        await query.message.edit_text(
            about_text,
            reply_markup=ABOUT_KEYBOARD,
            parse_mode=enums.ParseMode.HTML
        )

    await query.answer(UI("Showing about info."))


# -------------------------------------------------
# CLOSE MENU
# -------------------------------------------------

async def close_menu_handler(_, query: CallbackQuery):

    try:
        await query.message.delete()
        await query.answer(UI("Closed."))
    except Exception:
        await query.answer(UI("Can't close."))
