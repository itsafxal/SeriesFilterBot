import logging
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    CallbackQuery,
    InputMediaPhoto,
    Message
)
from pyrogram.errors import MessageNotModified

from config import DEFAULT_POSTER, ADMIN_USERS
from utils.cyber_font import BTN, UI

from DB.database import (
    get_index_channels,
    set_index_channels,
    get_global_setting,
    set_global_setting
)

logger = logging.getLogger(__name__)

# =====================================================
# TEMP ADMIN STATE (RAM BASED)
# =====================================================
admin_temp = {}

# =====================================================
# SAFE EDIT FUNCTION (Prevents MESSAGE_NOT_MODIFIED)
# =====================================================
async def safe_edit(message, text=None, caption=None, reply_markup=None):

    try:
        if caption is not None:
            await message.edit_caption(
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        elif text is not None:
            await message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )

    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Edit error: {e}")

# =====================================================
# HELP MENU
# =====================================================
async def help_menu_handler(cyber: Client, query: CallbackQuery):

    text = (
        f"<b>{UI('💡 Help & Instructions')}</b>\n\n"
        f"{UI('1. Send series name.')}\n"
        f"{UI('2. Select season.')}\n"
        f"{UI('3. Choose language & quality.')}\n"
        f"{UI('4. Download files.')}"
    )

    keyboard = InlineKeyboardMarkup([
        [BTN("🏡 HOME", callback_data="cyber|"),
         BTN("🛠 ADMIN", callback_data="admin|menu")]
    ])

    await safe_edit(query.message, caption=text, reply_markup=keyboard)
    await query.answer()

# =====================================================
# HOME MENU
# =====================================================
async def cyber_menu_handler(cyber: Client, query: CallbackQuery):

    user = query.from_user
    bot = await cyber.get_me()

    text = (
        f"<b>{UI(f'Hey {user.first_name}!')}</b>\n\n"
        f"{UI(f'Welcome to {bot.first_name}')}\n\n"
        f"{UI('Send series name to start.')}"
    )

    keyboard = InlineKeyboardMarkup([
        [BTN("🎞 Browse Library",
             switch_inline_query_current_chat="")],
        [BTN("⚡ Help", callback_data="help|menu"),
         BTN("About ✨", callback_data="about|menu")]
    ])

    try:
        await query.message.edit_media(
            InputMediaPhoto(
                media=DEFAULT_POSTER,
                caption=text,
                parse_mode=enums.ParseMode.HTML
            ),
            reply_markup=keyboard
        )
    except MessageNotModified:
        pass
    except Exception:
        await safe_edit(query.message, caption=text, reply_markup=keyboard)

    await query.answer()

# =====================================================
# ABOUT MENU
# =====================================================
async def about_menu_handler(_, query: CallbackQuery):

    text = (
        "<b>Series Bot</b>\n\n"
        "Library: Pyrogram\n"
        "Database: MongoDB\n"
        "Language: Python"
    )

    keyboard = InlineKeyboardMarkup([
        [BTN("🏡 HOME", callback_data="cyber|"),
         BTN("⚡ HELP", callback_data="help|menu")]
    ])

    await safe_edit(query.message, caption=text, reply_markup=keyboard)
    await query.answer()

# =====================================================
# ADMIN PANEL
# =====================================================
async def owner_menu_handler(_, query: CallbackQuery):

    if query.from_user.id not in ADMIN_USERS:
        return await query.answer("Not allowed.", show_alert=True)

    keyboard = InlineKeyboardMarkup([
        [BTN("⚙️ Global Settings", callback_data="gset|main")],
        [BTN("🏡 HOME", callback_data="cyber|")]
    ])

    await safe_edit(query.message,
                    caption="<b>🛠 Admin Panel</b>",
                    reply_markup=keyboard)

    await query.answer()

# =====================================================
# GLOBAL SETTINGS ROOT
# FIX: Previously only handled "gset|main" but the
# old adminsetting command sent "gset|index" etc.
# which had no handler. Now everything flows through
# gset|main → individual routers (index|, caption| etc.)
# =====================================================
async def global_settings_router(_, query: CallbackQuery):

    keyboard = InlineKeyboardMarkup([
        [BTN("📂 Index Channels", callback_data="index|index")],
        [BTN("📝 File Caption", callback_data="caption|caption")],
        [BTN("⏳ Auto Delete", callback_data="autodel|autodel")],
        [BTN("📦 File Auto Delete", callback_data="filedel|filedel")],
        [BTN("🔙 Back", callback_data="admin|menu")]
    ])

    await safe_edit(query.message,
                    caption="<b>⚙️ Global Settings</b>",
                    reply_markup=keyboard)

    await query.answer()

# =====================================================
# INDEX ROUTER
# FIX: Added reply_markup with back button to "list"
# action so users are not left stuck with no navigation.
# =====================================================
async def index_action_router(_, query: CallbackQuery):

    action = query.data.split("|")[1]

    if action == "index":

        keyboard = InlineKeyboardMarkup([
            [BTN("➕ Add", callback_data="index|add"),
             BTN("➖ Delete", callback_data="index|del")],
            [BTN("📋 List", callback_data="index|list")],
            [BTN("🔙 Back", callback_data="gset|main")]
        ])

        await safe_edit(query.message,
                        caption="<b>📂 Index Settings</b>",
                        reply_markup=keyboard)

    elif action == "list":

        channels = await get_index_channels()

        if not channels:
            text = "<b>No index channels set.</b>"
        else:
            text = "<b>📂 Index Channels:</b>\n\n"
            text += "\n".join([f"<code>{c}</code>" for c in channels])

        # FIX: Added back button — previously left user with no navigation
        back_kb = InlineKeyboardMarkup([
            [BTN("🔙 Back", callback_data="index|index")]
        ])

        await safe_edit(query.message, caption=text, reply_markup=back_kb)

    elif action == "add":
        admin_temp[query.from_user.id] = "WAITING_INDEX_ADD"
        await safe_edit(query.message,
                        caption="<b>Send channel ID to add.</b>")

    elif action == "del":
        admin_temp[query.from_user.id] = "WAITING_INDEX_DEL"
        await safe_edit(query.message,
                        caption="<b>Send channel ID to delete.</b>")

    await query.answer()

# =====================================================
# CAPTION ROUTER
# FIX: Added reply_markup with back button to "view"
# action so users are not left stuck with no navigation.
# =====================================================
async def caption_action_router(_, query: CallbackQuery):

    action = query.data.split("|")[1]
    KEY = "CUSTOM_FILE_CAPTION"

    if action == "caption":

        keyboard = InlineKeyboardMarkup([
            [BTN("👁 View", callback_data="caption|view")],
            [BTN("✏️ Set", callback_data="caption|set")],
            [BTN("🗑 Delete", callback_data="caption|delete")],
            [BTN("🔙 Back", callback_data="gset|main")]
        ])

        await safe_edit(query.message,
                        caption="<b>📝 Caption Settings</b>",
                        reply_markup=keyboard)

    elif action == "view":
        current = await get_global_setting(KEY, None)
        text = "<b>No custom caption set.</b>" \
            if not current else f"<b>Current Caption:</b>\n\n{current}"

        # FIX: Added back button — previously left user with no navigation
        back_kb = InlineKeyboardMarkup([
            [BTN("🔙 Back", callback_data="caption|caption")]
        ])

        await safe_edit(query.message, caption=text, reply_markup=back_kb)

    elif action == "set":
        admin_temp[query.from_user.id] = "WAITING_CAPTION"
        await safe_edit(query.message,
                        caption="<b>Send new caption now.</b>")

    elif action == "delete":
        await set_global_setting(KEY, None)

        back_kb = InlineKeyboardMarkup([
            [BTN("🔙 Back", callback_data="caption|caption")]
        ])

        await safe_edit(query.message,
                        caption="<b>Caption reset.</b>",
                        reply_markup=back_kb)

    await query.answer()

# =====================================================
# AUTODEL ROUTER
# =====================================================
async def autodel_action_router(_, query: CallbackQuery):

    action = query.data.split("|")[1]
    KEY = "AUTO_DELETE_DELAY"

    if action == "autodel":

        keyboard = InlineKeyboardMarkup([
            [BTN("✏️ Set Seconds", callback_data="autodel|set")],
            [BTN("🛑 Disable", callback_data="autodel|off")],
            [BTN("🔙 Back", callback_data="gset|main")]
        ])

        await safe_edit(query.message,
                        caption="<b>⏳ Auto Delete</b>",
                        reply_markup=keyboard)

    elif action == "set":
        admin_temp[query.from_user.id] = "WAITING_AUTODEL"
        await safe_edit(query.message,
                        caption="<b>Send seconds value.</b>")

    elif action == "off":
        await set_global_setting(KEY, 0)

        back_kb = InlineKeyboardMarkup([
            [BTN("🔙 Back", callback_data="autodel|autodel")]
        ])

        await safe_edit(query.message,
                        caption="<b>Auto delete disabled.</b>",
                        reply_markup=back_kb)

    await query.answer()

# =====================================================
# FILE AUTODEL ROUTER
# =====================================================
async def filedel_action_router(_, query: CallbackQuery):

    action = query.data.split("|")[1]
    KEY = "FILE_AUTODEL_DELAY"

    if action == "filedel":

        keyboard = InlineKeyboardMarkup([
            [BTN("✏️ Set Seconds", callback_data="filedel|set")],
            [BTN("🛑 Disable", callback_data="filedel|off")],
            [BTN("🔙 Back", callback_data="gset|main")]
        ])

        await safe_edit(query.message,
                        caption="<b>📦 File Auto Delete</b>",
                        reply_markup=keyboard)

    elif action == "set":
        admin_temp[query.from_user.id] = "WAITING_FILE_AUTODEL"
        await safe_edit(query.message,
                        caption="<b>Send seconds value.</b>")

    elif action == "off":
        await set_global_setting(KEY, 0)

        back_kb = InlineKeyboardMarkup([
            [BTN("🔙 Back", callback_data="filedel|filedel")]
        ])

        await safe_edit(query.message,
                        caption="<b>File auto delete disabled.</b>",
                        reply_markup=back_kb)

    await query.answer()

# =====================================================
# ADMIN INPUT HANDLER (plain async function — no decorator)
# FIX: Removed @Client.on_message class-level decorator.
# It is now registered properly inside register_menu_handlers()
# via the cyber instance, just like all other handlers.
# =====================================================
async def admin_input_handler(client, message: Message):

    user_id = message.from_user.id

    if user_id not in admin_temp:
        return

    state = admin_temp[user_id]

    try:
        if state == "WAITING_INDEX_ADD":
            cid = int(message.text)
            channels = await get_index_channels()
            channels.append(cid)
            await set_index_channels(channels)
            await message.reply("✅ Index channel added.")

        elif state == "WAITING_INDEX_DEL":
            cid = int(message.text)
            channels = await get_index_channels()
            if cid in channels:
                channels.remove(cid)
                await set_index_channels(channels)
                await message.reply("🗑 Index channel removed.")
            else:
                await message.reply("⚠️ Channel not found.")

        elif state == "WAITING_CAPTION":
            await set_global_setting("CUSTOM_FILE_CAPTION", message.text)
            await message.reply("✅ Caption updated.")

        elif state == "WAITING_AUTODEL":
            seconds = int(message.text)
            await set_global_setting("AUTO_DELETE_DELAY", seconds)
            await message.reply(f"✅ Auto delete set to {seconds}s.")

        elif state == "WAITING_FILE_AUTODEL":
            seconds = int(message.text)
            await set_global_setting("FILE_AUTODEL_DELAY", seconds)
            await message.reply(f"✅ File auto delete set to {seconds}s.")

    except ValueError:
        await message.reply("⚠️ Invalid input. Please send a valid value.")
    except Exception as e:
        logger.error(f"Admin input error: {e}")
        await message.reply("❌ Something went wrong.")

    finally:
        # Always clear state even on error so admin isn't stuck
        admin_temp.pop(user_id, None)

# =====================================================
# REGISTER CALLBACKS
# =====================================================
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
        await owner_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex("^gset\\|"))
    async def _(c, q):
        await global_settings_router(c, q)

    @cyber.on_callback_query(filters.regex("^index\\|"))
    async def _(c, q):
        await index_action_router(c, q)

    @cyber.on_callback_query(filters.regex("^caption\\|"))
    async def _(c, q):
        await caption_action_router(c, q)

    @cyber.on_callback_query(filters.regex("^autodel\\|"))
    async def _(c, q):
        await autodel_action_router(c, q)

    @cyber.on_callback_query(filters.regex("^filedel\\|"))
    async def _(c, q):
        await filedel_action_router(c, q)

    # FIX: Added handler for close|menu callback which was used
    # in adminsetting command but had no handler — caused dead button.
    @cyber.on_callback_query(filters.regex("^close\\|menu$"))
    async def _(c, q):
        try:
            await q.message.delete()
        except Exception:
            pass
        await q.answer()

    # FIX: admin_input_handler now registered here via the cyber instance
    # instead of using the broken @Client.on_message class-level decorator.
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS))
    async def _(c, m):
        await admin_input_handler(c, m)
