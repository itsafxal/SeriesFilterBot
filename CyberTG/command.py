import io
import asyncio

from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup

from utils.cyber_filter import decode_payload_async
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
    delete_all_files,
    delete_one_file,
)

from .files import deliver_files_direct, set_session
from .fsub import (
    check_fsub,
    get_slot_statuses,
    build_fsub_keyboard,
    fsub_text,
    register_pending_fsub,
    clear_pending_fsub,
    _PENDING_FSUB,
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

                # ── Get per-slot status for this user ──────────
                statuses = await get_slot_statuses(cyber, uid)
                active_slots = [s for s in statuses]  # all configured slots

                # User passes only when actually joined ALL slots (not just requested)
                passed = all(s["joined"] for s in active_slots)

                if not passed:
                    bot = await cyber.get_me()

                    # ── Always delete the /start message silently ──
                    try:
                        await message.delete()
                    except Exception:
                        pass

                    # ── If a gate message already exists for this user,
                    #    just edit it in place — never send a duplicate. ──
                    existing = _PENDING_FSUB.get(uid)
                    if existing and existing["payload"] == payload:
                        # Same link — update the existing gate message keyboard
                        # to reflect latest slot statuses, then stop.
                        kb = build_fsub_keyboard(bot.username, payload, statuses)
                        txt = fsub_text(statuses)
                        try:
                            await existing["msg"].edit_text(
                                txt,
                                reply_markup=kb,
                                parse_mode=enums.ParseMode.HTML,
                            )
                        except Exception:
                            pass
                        return

                    # ── No existing gate message — send a fresh one ──
                    kb  = build_fsub_keyboard(bot.username, payload, statuses)
                    txt = fsub_text(statuses)

                    gate_msg = await cyber.send_message(
                        uid,
                        txt,
                        reply_markup=kb,
                        parse_mode=enums.ParseMode.HTML,
                    )

                    # Register so join_request event can edit this message
                    register_pending_fsub(uid, gate_msg, payload)
                    return

                # ── User passed — clean up gate message then deliver ──
                # Delete the old fsub gate message (with ⏳/✅ buttons) so it
                # doesn't linger after the user has fully joined.
                pending = _PENDING_FSUB.get(uid)
                if pending:
                    try:
                        await pending["msg"].delete()
                    except Exception:
                        pass
                    clear_pending_fsub(uid)

                try:
                    decoded = await decode_payload_async(payload)
                    if not decoded:
                        await message.reply(
                            "⚠️ This link has expired or is invalid.\n"
                            "Please search for the series again to get a new link."
                        )
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        return

                    owner, series, season, lang, quality = decoded

                    if int(owner) != uid:
                        await message.reply("⚠️ This link is not for you.")
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        return

                    season = int(season)

                except Exception as e:
                    logger.error(f"PAYLOAD ERROR: {e}")
                    await message.reply("⚠️ Invalid link. Please try again.")
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    return

                set_session(uid, {
                    "series":   series,
                    "season":   season,
                    "language": lang,
                    "quality":  quality,
                })
                await deliver_files_direct(cyber, uid)

                try:
                    await message.delete()
                except Exception:
                    pass
                return

        # ── normal /start ─────────────────────────────────────
        bot       = await cyber.get_me()
        user      = message.from_user
        user_name = user.first_name

        lets_dive  = UI("Let's dive into entertainment 🌙")
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
    # /debugfsub
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("debugfsub") & filters.user(ADMIN_USERS))
    async def debug_fsub(_, message: Message):
        from CyberTG.fsub import (
            FSUB_CHANNEL1, FSUB_CHANNEL2,
            FSUB_MODE1, FSUB_MODE2,
            FSUB_LINK1, FSUB_LINK2,
            _join_reqs_db,
        )

        lines = ["<b>🔍 FSUB DEBUG REPORT</b>\n"]

        for slot, ch, mode, link in [
            (1, FSUB_CHANNEL1, FSUB_MODE1, FSUB_LINK1),
            (2, FSUB_CHANNEL2, FSUB_MODE2, FSUB_LINK2),
        ]:
            lines.append(f"<b>── Slot {slot} ──</b>")
            lines.append(f"  channel_id : <code>{ch}</code>")
            lines.append(f"  mode       : <code>{mode}</code>")
            lines.append(f"  link       : <code>{link}</code>")

            if ch:
                col   = _join_reqs_db[str(ch)]
                count = await asyncio.to_thread(lambda: col.count_documents({}))
                lines.append(f"  DB records : <code>{count}</code>")

                docs = await asyncio.to_thread(
                    lambda: list(col.find().sort("date", -1).limit(5))
                )
                if docs:
                    lines.append("  Last 5 requesters:")
                    for d in docs:
                        lines.append(
                            f"    • uid=<code>{d.get('user_id')}</code>"
                            f"  name=<code>{d.get('first_name','?')}</code>"
                            f"  @{d.get('username','?')}"
                            f"  {d.get('date','')}"
                        )
                else:
                    lines.append("  ⚠️ <b>DB is EMPTY</b>")
            else:
                lines.append("  <i>Not configured</i>")

            lines.append("")

        await message.reply(
            "\n".join(lines),
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )

    # ──────────────────────────────────────────────────────────
    # /checkuser <user_id>
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("checkuser") & filters.user(ADMIN_USERS))
    async def check_user_fsub(_, message: Message):
        from CyberTG.fsub import (
            FSUB_CHANNEL1, FSUB_CHANNEL2,
            FSUB_MODE1, FSUB_MODE2,
            _join_reqs_db,
        )

        uid = None
        if message.reply_to_message and message.reply_to_message.from_user:
            uid = message.reply_to_message.from_user.id
        elif len(message.command) > 1:
            try:
                uid = int(message.command[1])
            except ValueError:
                return await message.reply("Usage: /checkuser <user_id>  or reply to a message")
        else:
            return await message.reply("Usage: /checkuser <user_id>  or reply to a message")

        lines = [f"<b>🔍 Checking uid={uid}</b>\n"]

        for slot, ch, mode in [
            (1, FSUB_CHANNEL1, FSUB_MODE1),
            (2, FSUB_CHANNEL2, FSUB_MODE2),
        ]:
            lines.append(f"<b>Slot {slot}</b> ch=<code>{ch}</code> mode=<code>{mode}</code>")
            if not ch:
                lines.append("  → not configured\n")
                continue

            col = _join_reqs_db[str(ch)]
            doc = await asyncio.to_thread(lambda: col.find_one({"user_id": int(uid)}))
            lines.append(f"  MongoDB : <code>{'✅ FOUND' if doc else '❌ NOT FOUND'}</code>")

            try:
                member = await cyber.get_chat_member(ch, uid)
                lines.append(f"  Telegram: <code>✅ status={member.status}</code>")
            except Exception as e:
                lines.append(f"  Telegram: <code>❌ {e}</code>")

            lines.append("")

        passed = await check_fsub(cyber, uid)
        lines.append(
            f"<b>Final check_fsub result: {'✅ PASS' if passed else '❌ FAIL'}</b>"
        )

        await message.reply("\n".join(lines), parse_mode=enums.ParseMode.HTML)

    # ──────────────────────────────────────────────────────────
    # /testjoin
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("testjoin") & filters.user(ADMIN_USERS))
    async def test_join(_, message: Message):
        from CyberTG.fsub import _add_req, FSUB_CHANNEL1, FSUB_CHANNEL2
        uid = message.from_user.id
        fn  = message.from_user.first_name or ""
        un  = message.from_user.username   or ""
        results = []
        for slot, ch in [(1, FSUB_CHANNEL1), (2, FSUB_CHANNEL2)]:
            if ch:
                await _add_req(slot, uid, fn, un)
                results.append(f"  Slot {slot} ch=<code>{ch}</code> → uid=<code>{uid}</code> added")
        if results:
            await message.reply(
                "✅ <b>Manually added to DB:</b>\n" + "\n".join(results) +
                "\n\nNow try clicking your download link again.",
                parse_mode=enums.ParseMode.HTML
            )
        else:
            await message.reply("❌ No FSub channels configured.")

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
        await msg.reply("✅ Broadcast completed.")

    # ──────────────────────────────────────────────────────────
    # /total
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("total") & filters.user(ADMIN_USERS))
    async def total(_, m: Message):
        users = await get_total_users()
        files = await get_total_files()
        await m.reply(
            f"👤 Users: <code>{users}</code>\n📂 Files: <code>{files}</code>",
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
        cid = int(m.command[1])
        mid = int(m.command[2])
        await delete_one_file(cid, mid)
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
