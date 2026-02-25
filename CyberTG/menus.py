import logging
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    CallbackQuery,
    InputMediaPhoto,
    Message,
)
from pyrogram.errors import MessageNotModified

from config import DEFAULT_POSTER, ADMIN_USERS
from utils.cyber_font import BTN, UI

from DB.database import (
    get_index_channels,
    set_index_channels,
    get_global_setting,
    set_global_setting,
)

from .fsub import (
    set_fsub_slot,
    del_fsub_slot,
    set_threshold,
    purge_slot_reqs,
    get_slot_info,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# SHARED STATE
# ══════════════════════════════════════════════════════════════

admin_temp  = {}   # { user_id: { state, message, back } }
_fsub_state = {}   # { user_id: { step, slot, secondary, mode, msg } }


# ══════════════════════════════════════════════════════════════
# SAFE EDIT
# ══════════════════════════════════════════════════════════════

async def safe_edit(message, text=None, caption=None, reply_markup=None):
    try:
        if caption is not None:
            await message.edit_caption(
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML,
            )
        elif text is not None:
            await message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML,
            )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Edit error: {e}")


# ══════════════════════════════════════════════════════════════
# FSUB UI HELPERS
# ══════════════════════════════════════════════════════════════

def _fsub_main_keyboard():
    return InlineKeyboardMarkup([
        [BTN("1️⃣ FSub Slot 1", callback_data="fsub|slot1"),
         BTN("2️⃣ FSub Slot 2", callback_data="fsub|slot2")],
        [BTN("📊 View Status",  callback_data="fsub|view")],
        [BTN("🔙 Back",         callback_data="gset|main")],
    ])


def _fsub_slot_keyboard(slot: int):
    s = str(slot)
    return InlineKeyboardMarkup([
        [BTN("✅ Set Primary",    callback_data=f"fsub|set_primary_{s}"),
         BTN("🔄 Set Secondary", callback_data=f"fsub|set_secondary_{s}")],
        [BTN("🔢 Set Threshold", callback_data=f"fsub|set_thr_{s}")],
        [BTN("🗑 Del Primary",   callback_data=f"fsub|del_primary_{s}"),
         BTN("🗑 Del Secondary", callback_data=f"fsub|del_secondary_{s}")],
        [BTN("🧹 Purge Records", callback_data=f"fsub|purge_{s}")],
        [BTN("🔙 Back",          callback_data="fsub|main")],
    ])


async def _create_invite_link(bot, chat_id: int, mode: str):
    """Create request-to-join or normal invite link."""
    try:
        obj = await bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=(mode == "request"),
        )
        return obj.invite_link
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# MENU HANDLERS
# ══════════════════════════════════════════════════════════════

async def help_menu_handler(cyber, query):
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


async def cyber_menu_handler(cyber, query):
    user = query.from_user
    bot  = await cyber.get_me()
    text = (
        f"<b>{UI(f'Hey {user.first_name}!')}</b>\n\n"
        f"{UI(f'Welcome to {bot.first_name}')}\n\n"
        f"{UI('Send series name to start.')}"
    )
    keyboard = InlineKeyboardMarkup([
        [BTN("🎞 Browse Library", switch_inline_query_current_chat="")],
        [BTN("⚡ Help",  callback_data="help|menu"),
         BTN("About ✨", callback_data="about|menu")]
    ])
    try:
        await query.message.edit_media(
            InputMediaPhoto(media=DEFAULT_POSTER, caption=text,
                            parse_mode=enums.ParseMode.HTML),
            reply_markup=keyboard,
        )
    except MessageNotModified:
        pass
    except Exception:
        await safe_edit(query.message, caption=text, reply_markup=keyboard)
    await query.answer()


async def about_menu_handler(_, query):
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


async def owner_menu_handler(_, query):
    if query.from_user.id not in ADMIN_USERS:
        return await query.answer("Not allowed.", show_alert=True)
    keyboard = InlineKeyboardMarkup([
        [BTN("⚙️ Global Settings", callback_data="gset|main")],
        [BTN("🏡 HOME",            callback_data="cyber|")]
    ])
    await safe_edit(query.message, caption="<b>🛠 Admin Panel</b>",
                    reply_markup=keyboard)
    await query.answer()


async def global_settings_router(_, query):
    cap_lang = await get_global_setting("CAPTION_LANG", True)
    cap_lang_status = "✅ ON" if cap_lang else "❌ OFF"
    keyboard = InlineKeyboardMarkup([
        [BTN("📂 Index Channels",   callback_data="index|index")],
        [BTN("📝 File Caption",     callback_data="caption|caption")],
        [BTN("⏳ Auto Delete",      callback_data="autodel|autodel")],
        [BTN("📦 File Auto Delete", callback_data="filedel|filedel")],
        [BTN("📡 FSub Settings",    callback_data="fsub|main")],
        [BTN(f"🌐 Caption Language  {cap_lang_status}", callback_data="caplang|toggle")],
        [BTN("🔙 Back",             callback_data="admin|menu")],
    ])
    await safe_edit(query.message, caption="<b>⚙️ Global Settings</b>",
                    reply_markup=keyboard)
    await query.answer()


# ══════════════════════════════════════════════════════════════
# FSUB ADMIN ROUTER
# ══════════════════════════════════════════════════════════════

async def fsub_router(bot, query: CallbackQuery):
    await query.answer()
    action = query.data.split("|", 1)[1]
    uid    = query.from_user.id

    if action == "main":
        await safe_edit(
            query.message,
            caption="<b>📡 FSub Settings</b>\n\nChoose a slot to configure.",
            reply_markup=_fsub_main_keyboard(),
        )

    elif action in ("slot1", "slot2"):
        slot = int(action[-1])
        info = await get_slot_info(bot, slot)
        ch   = info["ch"]

        if ch:
            db_line = (f"🗂 DB Records: <code>{info['db_count']}</code>\n"
                       if info["db_count"] is not None else "")
            status = (
                f"🆔 <code>{ch}</code>\n"
                f"📛 {info['title']}\n"
                f"🔗 <a href='{info['link']}'>Invite Link</a>\n"
                f"⚙️ Mode: <code>{info['mode']}</code>\n"
                f"🔄 Secondary: <code>{info['ch2'] or 'Not set'}</code>\n"
                f"📊 Threshold: <code>{info['threshold'] or 'Not set'}</code>\n"
                f"👥 Counter: <code>{info['counter']}</code>\n"
                + db_line
            )
        else:
            status = "_Not configured._"

        await safe_edit(
            query.message,
            caption=f"<b>📡 Slot {slot}</b>\n\n{status}",
            reply_markup=_fsub_slot_keyboard(slot),
        )

    elif action.startswith("set_primary_"):
        slot = int(action[-1])
        _fsub_state[uid] = {"step": "ask_mode", "slot": slot, "secondary": False}
        await safe_edit(
            query.message,
            caption=f"<b>Slot {slot} — Set Primary</b>\n\nChoose join mode:",
            reply_markup=InlineKeyboardMarkup([
                [BTN("📢 Request to Join", callback_data=f"fsub|mode_request_{slot}_0"),
                 BTN("🔗 Normal Invite",   callback_data=f"fsub|mode_invite_{slot}_0")],
                [BTN("🔙 Back",            callback_data=f"fsub|slot{slot}")],
            ]),
        )

    elif action.startswith("set_secondary_"):
        slot = int(action[-1])
        _fsub_state[uid] = {"step": "ask_mode", "slot": slot, "secondary": True}
        await safe_edit(
            query.message,
            caption=f"<b>Slot {slot} — Set Secondary</b>\n\nChoose join mode:",
            reply_markup=InlineKeyboardMarkup([
                [BTN("📢 Request to Join", callback_data=f"fsub|mode_request_{slot}_1"),
                 BTN("🔗 Normal Invite",   callback_data=f"fsub|mode_invite_{slot}_1")],
                [BTN("🔙 Back",            callback_data=f"fsub|slot{slot}")],
            ]),
        )

    elif action.startswith("mode_"):
        parts     = action.split("_")
        mode      = parts[1]
        slot      = int(parts[2])
        secondary = parts[3] == "1"
        label     = "Secondary" if secondary else "Primary"
        m_label   = "Request to Join" if mode == "request" else "Normal Invite"

        _fsub_state[uid] = {
            "step":      "wait_channel_id",
            "slot":      slot,
            "secondary": secondary,
            "mode":      mode,
            "msg":       query.message,
        }
        await safe_edit(
            query.message,
            caption=(
                f"<b>Slot {slot} — {label}</b>\n"
                f"Mode: <b>{m_label}</b>\n\n"
                "Send the <b>channel ID</b>\n"
                "e.g. <code>-1001234567890</code>"
            ),
        )

    elif action.startswith("set_thr_"):
        slot = int(action[-1])
        _fsub_state[uid] = {
            "step": "wait_threshold",
            "slot": slot,
            "msg":  query.message,
        }
        await safe_edit(
            query.message,
            caption=(
                f"<b>Slot {slot} — Set Threshold</b>\n\n"
                "Send the <b>number</b> of join requests before the channel rotates to secondary."
            ),
        )

    elif action.startswith("del_primary_"):
        slot = int(action[-1])
        old  = await del_fsub_slot(slot, secondary=False)
        await safe_edit(
            query.message,
            caption=f"🗑 Slot {slot} primary removed.\nWas: <code>{old}</code>",
            reply_markup=_fsub_slot_keyboard(slot),
        )

    elif action.startswith("del_secondary_"):
        slot = int(action[-1])
        old  = await del_fsub_slot(slot, secondary=True)
        await safe_edit(
            query.message,
            caption=f"🗑 Slot {slot} secondary removed.\nWas: <code>{old}</code>",
            reply_markup=_fsub_slot_keyboard(slot),
        )

    elif action.startswith("purge_"):
        slot = int(action[-1])
        await purge_slot_reqs(slot)
        await safe_edit(
            query.message,
            caption=f"🧹 Slot {slot} — all records purged, counter reset to 0.",
            reply_markup=_fsub_slot_keyboard(slot),
        )

    elif action == "view":
        lines = ["<b>📊 FSub Status</b>\n"]
        for slot in (1, 2):
            info = await get_slot_info(bot, slot)
            if info["ch"]:
                db_line = (f"  🗂 DB Records: <code>{info['db_count']}</code>\n"
                           if info["db_count"] is not None else "")
                lines.append(
                    f"<b>── Slot {slot} ──</b>\n"
                    f"  🆔 <code>{info['ch']}</code>\n"
                    f"  📛 {info['title']}\n"
                    f"  ⚙️ Mode: <code>{info['mode']}</code>\n"
                    f"  🔄 Secondary: <code>{info['ch2'] or 'Not set'}</code>\n"
                    f"  📊 Threshold: <code>{info['threshold'] or 'Not set'}</code>\n"
                    f"  👥 Counter: <code>{info['counter']}</code>\n"
                    + db_line
                )
            else:
                lines.append(f"<b>── Slot {slot} ──</b>\n  <i>Not configured.</i>\n")

        await safe_edit(
            query.message,
            caption="\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [BTN("🔙 Back", callback_data="fsub|main")]
            ]),
        )


# ══════════════════════════════════════════════════════════════
# OTHER SETTINGS ROUTERS
# ══════════════════════════════════════════════════════════════

async def index_action_router(_, query):
    action = query.data.split("|")[1]

    if action == "index":
        keyboard = InlineKeyboardMarkup([
            [BTN("➕ Add",    callback_data="index|add"),
             BTN("➖ Delete", callback_data="index|del")],
            [BTN("📋 List",   callback_data="index|list")],
            [BTN("🔙 Back",   callback_data="gset|main")],
        ])
        await safe_edit(query.message, caption="<b>📂 Index Settings</b>",
                        reply_markup=keyboard)

    elif action == "list":
        channels = await get_index_channels()
        text = ("<b>No index channels set.</b>" if not channels else
                "<b>📂 Index Channels:</b>\n\n" +
                "\n".join(f"<code>{c}</code>" for c in channels))
        await safe_edit(query.message, caption=text,
                        reply_markup=InlineKeyboardMarkup(
                            [[BTN("🔙 Back", callback_data="index|index")]]))

    elif action == "add":
        await safe_edit(query.message, caption="<b>📂 Send the channel ID to add.</b>")
        admin_temp[query.from_user.id] = {
            "state": "WAITING_INDEX_ADD",
            "message": query.message,
            "back": "index|index",
        }

    elif action == "del":
        await safe_edit(query.message, caption="<b>📂 Send the channel ID to delete.</b>")
        admin_temp[query.from_user.id] = {
            "state": "WAITING_INDEX_DEL",
            "message": query.message,
            "back": "index|index",
        }

    await query.answer()


async def caption_action_router(_, query):
    action = query.data.split("|")[1]
    KEY    = "CUSTOM_FILE_CAPTION"

    if action == "caption":
        keyboard = InlineKeyboardMarkup([
            [BTN("👁 View",   callback_data="caption|view")],
            [BTN("✏️ Set",    callback_data="caption|set")],
            [BTN("🗑 Delete", callback_data="caption|delete")],
            [BTN("🔙 Back",   callback_data="gset|main")],
        ])
        await safe_edit(query.message, caption="<b>📝 Caption Settings</b>",
                        reply_markup=keyboard)

    elif action == "view":
        current = await get_global_setting(KEY, None)
        text    = ("<b>No custom caption set.</b>" if not current else
                   f"<b>Current Caption:</b>\n\n{current}")
        await safe_edit(query.message, caption=text,
                        reply_markup=InlineKeyboardMarkup(
                            [[BTN("🔙 Back", callback_data="caption|caption")]]))

    elif action == "set":
        await safe_edit(query.message, caption="<b>📝 Send new caption now.</b>")
        admin_temp[query.from_user.id] = {
            "state": "WAITING_CAPTION",
            "message": query.message,
            "back": "caption|caption",
        }

    elif action == "delete":
        await set_global_setting(KEY, None)
        await safe_edit(query.message, caption="<b>Caption reset.</b>",
                        reply_markup=InlineKeyboardMarkup(
                            [[BTN("🔙 Back", callback_data="caption|caption")]]))

    await query.answer()


async def autodel_action_router(_, query):
    action = query.data.split("|")[1]
    KEY    = "AUTO_DELETE_DELAY"

    if action == "autodel":
        keyboard = InlineKeyboardMarkup([
            [BTN("✏️ Set Seconds", callback_data="autodel|set")],
            [BTN("🛑 Disable",     callback_data="autodel|off")],
            [BTN("🔙 Back",        callback_data="gset|main")],
        ])
        await safe_edit(query.message, caption="<b>⏳ Auto Delete</b>",
                        reply_markup=keyboard)

    elif action == "set":
        await safe_edit(query.message,
                        caption="<b>⏳ Send seconds value for auto delete.</b>")
        admin_temp[query.from_user.id] = {
            "state": "WAITING_AUTODEL",
            "message": query.message,
            "back": "autodel|autodel",
        }

    elif action == "off":
        await set_global_setting(KEY, 0)
        await safe_edit(query.message, caption="<b>Auto delete disabled.</b>",
                        reply_markup=InlineKeyboardMarkup(
                            [[BTN("🔙 Back", callback_data="autodel|autodel")]]))

    await query.answer()


async def filedel_action_router(_, query):
    action = query.data.split("|")[1]
    KEY    = "FILE_AUTODEL_DELAY"

    if action == "filedel":
        keyboard = InlineKeyboardMarkup([
            [BTN("✏️ Set Seconds", callback_data="filedel|set")],
            [BTN("🛑 Disable",     callback_data="filedel|off")],
            [BTN("🔙 Back",        callback_data="gset|main")],
        ])
        await safe_edit(query.message, caption="<b>📦 File Auto Delete</b>",
                        reply_markup=keyboard)

    elif action == "set":
        await safe_edit(query.message,
                        caption="<b>📦 Send seconds value for file auto delete.</b>")
        admin_temp[query.from_user.id] = {
            "state": "WAITING_FILE_AUTODEL",
            "message": query.message,
            "back": "filedel|filedel",
        }

    elif action == "off":
        await set_global_setting(KEY, 0)
        await safe_edit(query.message, caption="<b>File auto delete disabled.</b>",
                        reply_markup=InlineKeyboardMarkup(
                            [[BTN("🔙 Back", callback_data="filedel|filedel")]]))

    await query.answer()


# ══════════════════════════════════════════════════════════════
# UNIFIED ADMIN TEXT INPUT HANDLER
# ══════════════════════════════════════════════════════════════

async def admin_input_handler(bot, message: Message):
    uid = message.from_user.id

    # ── FSub multi-step input ─────────────────────────────────
    if uid in _fsub_state:
        state   = _fsub_state[uid]
        step    = state["step"]
        bot_msg = state.get("msg")

        try:
            await message.delete()
        except Exception:
            pass

        if step == "wait_channel_id":
            slot      = state["slot"]
            secondary = state["secondary"]
            mode      = state["mode"]

            try:
                channel_id = int(message.text.strip())
            except ValueError:
                await safe_edit(bot_msg, caption="❌ Invalid ID. Send a numeric channel ID.")
                _fsub_state.pop(uid, None)
                return

            try:
                chat = await bot.get_chat(channel_id)
            except Exception as e:
                await safe_edit(bot_msg,
                    caption=f"❌ Cannot access chat: <code>{e}</code>\nMake sure the bot is admin.")
                _fsub_state.pop(uid, None)
                return

            link = await _create_invite_link(bot, channel_id, mode)
            if not link:
                await safe_edit(bot_msg,
                    caption="❌ Failed to create invite link. Ensure bot has admin rights in that channel.")
                _fsub_state.pop(uid, None)
                return

            await set_fsub_slot(slot, channel_id, link, mode, secondary=secondary)

            label   = "Secondary" if secondary else "Primary"
            m_label = "Request to Join" if mode == "request" else "Normal Invite"

            await safe_edit(
                bot_msg,
                caption=(
                    f"✅ <b>Slot {slot} {label} Set!</b>\n\n"
                    f"🆔 <code>{channel_id}</code>\n"
                    f"📛 {chat.title}\n"
                    f"⚙️ Mode: <b>{m_label}</b>\n"
                    f"🔗 <a href='{link}'>Invite Link</a>"
                ),
                reply_markup=_fsub_slot_keyboard(slot),
            )

        elif step == "wait_threshold":
            slot = state["slot"]
            try:
                value = int(message.text.strip())
                if value <= 0:
                    raise ValueError
            except ValueError:
                await safe_edit(bot_msg, caption="❌ Send a positive integer.")
                _fsub_state.pop(uid, None)
                return

            await set_threshold(slot, value)
            await safe_edit(
                bot_msg,
                caption=(
                    f"✅ <b>Slot {slot} threshold → <code>{value}</code> requests.</b>\n\n"
                    "Channel will rotate to secondary once this count is exceeded."
                ),
                reply_markup=_fsub_slot_keyboard(slot),
            )

        _fsub_state.pop(uid, None)
        return

    # ── Existing settings input ───────────────────────────────
    # If admin has no pending input state, this is a normal search —
    # return without consuming the message (handled by search_handler)
    if uid not in admin_temp:
        return

    entry   = admin_temp[uid]
    state   = entry["state"]
    bot_msg = entry["message"]
    back    = entry["back"]

    try:
        await message.delete()
    except Exception:
        pass

    success_text = None
    error_text   = None

    try:
        if state == "WAITING_INDEX_ADD":
            cid      = int(message.text)
            channels = await get_index_channels()
            if cid in channels:
                error_text = "⚠️ Channel already exists."
            else:
                channels.append(cid)
                await set_index_channels(channels)
                success_text = f"✅ Index channel <code>{cid}</code> added."

        elif state == "WAITING_INDEX_DEL":
            cid      = int(message.text)
            channels = await get_index_channels()
            if cid in channels:
                channels.remove(cid)
                await set_index_channels(channels)
                success_text = f"🗑 Index channel <code>{cid}</code> removed."
            else:
                error_text = "⚠️ Channel not found."

        elif state == "WAITING_CAPTION":
            await set_global_setting("CUSTOM_FILE_CAPTION", message.text)
            success_text = "✅ Caption updated."

        elif state == "WAITING_AUTODEL":
            seconds = int(message.text)
            await set_global_setting("AUTO_DELETE_DELAY", seconds)
            success_text = f"✅ Auto delete set to <b>{seconds}s</b>."

        elif state == "WAITING_FILE_AUTODEL":
            seconds = int(message.text)
            await set_global_setting("FILE_AUTODEL_DELAY", seconds)
            success_text = f"✅ File auto delete set to <b>{seconds}s</b>."

    except ValueError:
        error_text = "⚠️ Invalid input."
    except Exception as e:
        logger.error(f"Admin input error: {e}")
        error_text = "❌ Something went wrong. Try again."
    finally:
        admin_temp.pop(uid, None)

    back_kb     = InlineKeyboardMarkup([[BTN("🔙 Back", callback_data=back)]])
    result_text = success_text if success_text else error_text
    await safe_edit(bot_msg, caption=result_text, reply_markup=back_kb)


# ══════════════════════════════════════════════════════════════
# CAPTION LANGUAGE TOGGLE
# ══════════════════════════════════════════════════════════════

async def caplang_toggle_router(_, query: CallbackQuery):
    """Toggle CAPTION_LANG on/off and refresh Global Settings."""
    current = await get_global_setting("CAPTION_LANG", True)
    # Flip the value
    new_val = not current
    await set_global_setting("CAPTION_LANG", new_val)
    status  = "✅ ON" if new_val else "❌ OFF"
    await query.answer(f"Caption Language Detection: {status}", show_alert=True)
    # Refresh the Global Settings page with updated button
    await global_settings_router(_, query)


# ══════════════════════════════════════════════════════════════
# REGISTER ALL HANDLERS
# ══════════════════════════════════════════════════════════════

# All bot commands that should NOT be intercepted by admin_input_handler
_ALL_COMMANDS = [
    "start", "broadcast", "total", "deleteall", "deletefile",
    "logs", "adminsetting", "indexlink", "setskip", "getskip",
]

def register_menu_handlers(cyber: Client):

    @cyber.on_callback_query(filters.regex(r"^help\|menu$"))
    async def _(c, q): await help_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex(r"^about\|menu$"))
    async def _(c, q): await about_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex(r"^cyber\|"))
    async def _(c, q): await cyber_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex(r"^admin\|menu$"))
    async def _(c, q): await owner_menu_handler(c, q)

    @cyber.on_callback_query(filters.regex(r"^gset\|"))
    async def _(c, q): await global_settings_router(c, q)

    @cyber.on_callback_query(filters.regex(r"^index\|"))
    async def _(c, q): await index_action_router(c, q)

    @cyber.on_callback_query(filters.regex(r"^caption\|"))
    async def _(c, q): await caption_action_router(c, q)

    @cyber.on_callback_query(filters.regex(r"^autodel\|"))
    async def _(c, q): await autodel_action_router(c, q)

    @cyber.on_callback_query(filters.regex(r"^filedel\|"))
    async def _(c, q): await filedel_action_router(c, q)

    @cyber.on_callback_query(filters.regex(r"^fsub\|") & filters.user(ADMIN_USERS))
    async def _(c, q): await fsub_router(c, q)

    @cyber.on_callback_query(filters.regex(r"^caplang\|") & filters.user(ADMIN_USERS))
    async def _(c, q): await caplang_toggle_router(c, q)

    @cyber.on_callback_query(filters.regex(r"^close\|menu$"))
    async def _(c, q):
        try:
            await q.message.delete()
        except Exception:
            pass
        await q.answer()

    # Custom filter: only intercept admin text when they have a pending input state
    # This prevents the handler from swallowing normal admin search queries
    async def _has_admin_state(_, __, m):
        if not m.from_user:
            return False
        uid = m.from_user.id
        return uid in _fsub_state or uid in admin_temp

    has_admin_state = filters.create(_has_admin_state)

    @cyber.on_message(
        filters.private
        & filters.user(ADMIN_USERS)
        & filters.text
        & ~filters.command(_ALL_COMMANDS)
        & has_admin_state   # ← ONLY fires when admin has pending input
    )
    async def _(c, m): await admin_input_handler(c, m)
