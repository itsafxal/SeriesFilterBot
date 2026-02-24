"""
command.py — FIXED (works with new fsub.py)
============================================

/start with send_ payload:
  1. Get slot statuses
  2. If ANY slot not satisfied → show gate message → register pending fsub
  3. If ALL slots satisfied → check delivery lock
       • Lock FREE  → deliver (first time or genuine retry after delivery done)
       • Lock HELD  → silently skip (auto-deliver already running from join event)

  For invite-mode channels: get_slot_statuses() does the Telegram API check.
  If the user just joined, it returns joined=True → all_slots_passed=True → deliver.
  No join_request event needed for invite mode.
"""

import io
import asyncio

from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup

from utils.cyber_font import BTN, UI
from config import (
    logger,
    ADMIN_USERS,
    START_IMG,
    LOG_FILE_PATH,
    MAX_LOG_SIZE,
)
from DB.database import (
    add_user,
    get_all_users,
    get_total_users,
    get_total_files,
    get_total_series,
    get_total_files_per_db,
    get_all_series,
    delete_all_files,
    delete_one_file,
)
from utils.cyber_delete import delete_message_later
from .fsub import (
    get_slot_statuses,
    all_slots_passed,
    build_fsub_keyboard,
    fsub_text,
    register_pending_fsub,
    clear_pending_fsub,
    do_deliver,
    is_delivery_locked,
    _PENDING_FSUB,
    check_fsub,
)


def register_command_handlers(cyber: Client):

    # ──────────────────────────────────────────────────────────
    # /start
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):
        uid = message.from_user.id
        await add_user(uid)

        if len(message.command) > 1:
            payload = message.command[1]

            if payload.startswith("send_"):

                # Delete the /start message immediately (clean UX)
                try:
                    await message.delete()
                except Exception:
                    pass

                # Get real-time slot statuses
                statuses = await get_slot_statuses(cyber, uid)
                passed   = all_slots_passed(statuses)

                logger.info(
                    f"[START] uid={uid} passed={passed} "
                    f"statuses={[(s['slot'],s['joined'],s['requested']) for s in statuses]}"
                )

                if not passed:
                    # ── Show / refresh gate message ───────────
                    bot      = await cyber.get_me()
                    kb       = build_fsub_keyboard(bot.username, payload, statuses)
                    txt      = fsub_text(statuses)
                    existing = _PENDING_FSUB.get(uid)

                    if existing and existing["payload"] == payload:
                        # Refresh existing gate message keyboard
                        try:
                            await existing["msg"].edit_text(
                                txt, reply_markup=kb,
                                parse_mode=enums.ParseMode.HTML,
                            )
                        except Exception:
                            pass
                    else:
                        # Send fresh gate message
                        gate_msg = await cyber.send_message(
                            uid, txt, reply_markup=kb,
                            parse_mode=enums.ParseMode.HTML,
                        )
                        register_pending_fsub(uid, gate_msg, payload)
                    return

                # ── FSub passed — deliver ─────────────────────

                # Clean up any lingering gate message
                pending = _PENDING_FSUB.get(uid)
                if pending:
                    try:
                        await pending["msg"].delete()
                    except Exception:
                        pass
                    clear_pending_fsub(uid)

                # do_deliver handles:
                #   - delivery lock (prevents duplicate from concurrent auto-deliver)
                #   - session set
                #   - file delivery
                #   - retry button send
                # If locked → delivery is already running (auto-deliver beat us) → skip silently
                await do_deliver(cyber, uid, payload, source="start")
                return

        # ── Normal /start (no payload) ────────────────────────
        bot       = await cyber.get_me()
        user_name = message.from_user.first_name
        lets_dive = UI("Let's dive into entertainment 🌙")

        START_TEXT = (
            f"<b>{UI(f'Hey {user_name}!')}</b>\n\n"
            f"<b>{UI(f'Welcome to {bot.first_name}')}</b>\n\n"
            f"{UI('Your private cinema inside Telegram 🎬')}\n"
            f"{UI('Send any series name to begin.')}\n\n"
            f"<blockquote>{lets_dive}</blockquote>"
        )

        keyboard = InlineKeyboardMarkup([
            [BTN("🎞 ʙʀᴏᴡsᴇ ʟɪʙʀᴀʀʏ 🎞", switch_inline_query_current_chat="")],
            [BTN("⚡ ʜᴇʟᴘ",  callback_data="help|menu"),
             BTN("ᴀʙᴏᴜᴛ ✨", callback_data="about|menu")],
            [BTN("🍷 sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ 🍷", url="https://t.me/TomxSupport")],
        ])

        await message.reply_photo(
            START_IMG,
            caption=START_TEXT,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.HTML,
        )

    # ──────────────────────────────────────────────────────────
    # /broadcast
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("broadcast") & filters.user(ADMIN_USERS))
    async def broadcast(_, msg: Message):
        if not msg.reply_to_message:
            return await msg.reply("Reply to a message to broadcast it.")
        users = await get_all_users()
        for u in users:
            try:
                await msg.reply_to_message.copy(u)
                await asyncio.sleep(0.1)
            except Exception:
                pass
        await msg.reply("✅ Broadcast done.")

    # ──────────────────────────────────────────────────────────
    # /total
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("total") & filters.user(ADMIN_USERS))
    async def total(_, m: Message):
        users  = await get_total_users()
        files  = await get_total_files()
        series = await get_total_series()
        per_db = await get_total_files_per_db()

        db1 = per_db.get("db1", "?")
        db2 = per_db.get("db2", "?")

        await m.reply(
            f"<b>📊 Bot Statistics</b>\n\n"
            f"👤 <b>Users</b>          : <code>{users}</code>\n"
            f"📂 <b>Total Files</b>    : <code>{files}</code>\n"
            f"🎬 <b>Batched Series</b> : <code>{series}</code>\n\n"
            f"<b>📁 MongoDB File DBs</b>\n"
            f"  DB1 : <code>{db1}</code> files\n"
            f"  DB2 : <code>{db2}</code> files",
            parse_mode=enums.ParseMode.HTML,
        )

    # ──────────────────────────────────────────────────────────
    # /listbatch
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("listbatch") & filters.user(ADMIN_USERS))
    async def listbatch(_, m: Message):
        series_list = await get_all_series()

        if not series_list:
            return await m.reply("❌ No series found in database.")

        total_series = len(series_list)
        total_files  = sum(count for _, count in series_list)

        lines = [
            f"🎬 BATCHED SERIES LIST",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Total Series : {total_series}",
            f"Total Files  : {total_files}",
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for i, (name, count) in enumerate(series_list, 1):
            lines.append(f"{i:>4}. {name}  ({count} files)")

        content = "\n".join(lines)

        file_buf      = io.BytesIO(content.encode("utf-8"))
        file_buf.name = "batched_series.txt"

        await m.reply_document(
            document=file_buf,
            caption=(
                f"<b>🎬 Batched Series List</b>\n"
                f"Total Series: <code>{total_series}</code>\n"
                f"Total Files : <code>{total_files}</code>"
            ),
            parse_mode=enums.ParseMode.HTML,
        )

    # ──────────────────────────────────────────────────────────
    # /deleteall
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("deleteall") & filters.user(ADMIN_USERS))
    async def deleteall(_, m: Message):
        await delete_all_files()
        await m.reply("🗑️ All files deleted.")

    # ──────────────────────────────────────────────────────────
    # /deletefile <cid> <mid>
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("deletefile") & filters.user(ADMIN_USERS))
    async def deletefile(_, m: Message):
        if len(m.command) < 3:
            return await m.reply(
                "Usage: <code>/deletefile &lt;channel_id&gt; &lt;message_id&gt;</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        await delete_one_file(int(m.command[1]), int(m.command[2]))
        await m.reply("✅ File deleted.")

    # ──────────────────────────────────────────────────────────
    # /logs
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("logs") & filters.user(ADMIN_USERS))
    async def send_logs_cmd(_, message: Message):
        try:
            with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
                log_content = f.read()
            if not log_content:
                return await message.reply_text("🪵 Log file is empty.")
            if len(log_content.encode("utf-8")) > MAX_LOG_SIZE:
                log_content = "⚠️ Log truncated.\n\n" + log_content[-(MAX_LOG_SIZE // 2):]
            log_file      = io.BytesIO(log_content.encode("utf-8"))
            log_file.name = "bot_logs.txt"
            await message.reply_document(
                document=log_file,
                caption="🪵 **Current Bot Logs**",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Error sending logs: {e}")
            await message.reply("Failed to send logs.")

    # ──────────────────────────────────────────────────────────
    # /adminsetting
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("adminsetting") & filters.user(ADMIN_USERS))
    async def admin_settings(_, message: Message):
        keyboard = InlineKeyboardMarkup([
            [BTN("⚙️ Global Settings", callback_data="gset|main")],
            [BTN("❌ Close",           callback_data="close|menu")],
        ])
        await message.reply(
            "<b>🛠 Admin Panel</b>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard,
        )
