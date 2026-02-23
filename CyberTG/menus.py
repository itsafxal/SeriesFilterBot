from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, CallbackQuery
from config import ADMIN_USERS
from utils.cyber_font import BTN, UI

from DB.database import (
    get_index_channels,
    set_index_channels,
    get_global_setting,
    set_global_setting
)

import logging

logger = logging.getLogger(__name__)

# =============================================
# ADMIN TEMP STATE
# =============================================
admin_temp = {}


# =============================================
# ADMIN MAIN MENU
# =============================================
async def admin_menu_handler(_, query: CallbackQuery):

    if query.from_user.id not in ADMIN_USERS:
        return await query.answer("Not allowed.", show_alert=True)

    keyboard = InlineKeyboardMarkup([
        [BTN("📂 Index Channels", callback_data="gset|index")],
        [BTN("📝 File Caption", callback_data="gset|caption")],
        [
            BTN("⏳ Auto Delete", callback_data="gset|autodel"),
            BTN("📦 File Auto Delete", callback_data="gset|filedel"),
        ],
        [BTN("❌ Close", callback_data="close|menu")]
    ])

    await query.message.edit_text(
        "<b>⚙️ Global Settings Panel</b>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=keyboard
    )

    await query.answer()


# =============================================
# GLOBAL SETTINGS ROUTER
# =============================================
async def global_settings_router(_, query: CallbackQuery):

    action = query.data.split("|")[1]

    if action == "index":
        await index_menu(query)

    elif action == "caption":
        await caption_menu(query)

    elif action == "autodel":
        await autodel_menu(query)

    elif action == "filedel":
        await filedel_menu(query)

    await query.answer()


# =============================================
# INDEX MENU
# =============================================
async def index_menu(query: CallbackQuery):

    keyboard = InlineKeyboardMarkup([
        [
            BTN("➕ Add Index", callback_data="index|add"),
            BTN("➖ Delete Index", callback_data="index|del")
        ],
        [BTN("📋 List Index", callback_data="index|list")],
        [BTN("🔙 Back", callback_data="admin|menu")]
    ])

    await query.message.edit_text(
        "<b>📂 Index Channel Settings</b>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=keyboard
    )


# =============================================
# CAPTION MENU
# =============================================
async def caption_menu(query: CallbackQuery):

    keyboard = InlineKeyboardMarkup([
        [BTN("👁 View Caption", callback_data="caption|view")],
        [BTN("✏️ Set Caption", callback_data="caption|set")],
        [BTN("🗑 Delete Caption", callback_data="caption|delete")],
        [BTN("🔙 Back", callback_data="admin|menu")]
    ])

    await query.message.edit_text(
        "<b>📝 File Caption Settings</b>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=keyboard
    )


# =============================================
# AUTODEL MENU
# =============================================
async def autodel_menu(query: CallbackQuery):

    keyboard = InlineKeyboardMarkup([
        [BTN("✏️ Set Seconds", callback_data="autodel|set")],
        [BTN("🛑 Disable", callback_data="autodel|off")],
        [BTN("🔙 Back", callback_data="admin|menu")]
    ])

    await query.message.edit_text(
        "<b>⏳ Auto Delete Settings</b>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=keyboard
    )


# =============================================
# FILEDEL MENU
# =============================================
async def filedel_menu(query: CallbackQuery):

    keyboard = InlineKeyboardMarkup([
        [BTN("✏️ Set Seconds", callback_data="filedel|set")],
        [BTN("🛑 Disable", callback_data="filedel|off")],
        [BTN("🔙 Back", callback_data="admin|menu")]
    ])

    await query.message.edit_text(
        "<b>📦 File Auto Delete Settings</b>",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=keyboard
    )


# -------------------------------------------------
# HOME MENU
# -------------------------------------------------

async def cyber_menu_handler(cyber: Client, query: CallbackQuery):

    user = query.from_user
    bot = await cyber.get_me()
    user_name = user.first_name

    START_TEXT = (
        f"🖤 <b>{UI(f'Hey {user_name}!')}</b>\n\n"
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

async def owner_menu_handler(cyber: Client, query: CallbackQuery):

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

    # -------------------------------
    # HELP MENU
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^help\\|menu$"))
    async def _(c, q):
        await help_menu_handler(c, q)

    # -------------------------------
    # ABOUT MENU
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^about\\|menu$"))
    async def _(c, q):
        await about_menu_handler(c, q)

    # -------------------------------
    # HOME MENU
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^cyber\\|"))
    async def _(c, q):
        await cyber_menu_handler(c, q)

    # -------------------------------
    # ADMIN MAIN MENU
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^admin\\|menu$"))
    async def _(c, q):
        await owner_menu_handler(c, q)

    # -------------------------------
    # GLOBAL SETTINGS ROOT
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^gset\\|"))
    async def _(c, q):
        await global_settings_router(c, q)

    # -------------------------------
    # INDEX SETTINGS ACTIONS
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^index\\|"))
    async def _(c, q):
        await index_action_router(c, q)

    # -------------------------------
    # CAPTION SETTINGS ACTIONS
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^caption\\|"))
    async def _(c, q):
        await caption_action_router(c, q)

    # -------------------------------
    # AUTO DELETE SETTINGS
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^autodel\\|"))
    async def _(c, q):
        await autodel_action_router(c, q)

    # -------------------------------
    # FILE AUTO DELETE SETTINGS
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^filedel\\|"))
    async def _(c, q):
        await filedel_action_router(c, q)

    # -------------------------------
    # CLOSE MENU
    # -------------------------------
    @cyber.on_callback_query(filters.regex("^close\\|menu$"))
    async def _(c, q):
        await close_menu_handler(c, q)
