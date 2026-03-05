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
    get_global_setting,
    set_global_setting,
    sql_db_lock,
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

        # ── Maintenance mode check ─────────────────────────────
        if uid not in ADMIN_USERS:
            maintenance = await get_global_setting("MAINTENANCE_MODE", False)
            if maintenance:
                return await message.reply_text(
                    "🔧 <b>Bot is currently under maintenance.</b>\n\n"
                    "We'll be back shortly. Please try again later!",
                    parse_mode=enums.ParseMode.HTML,
                )

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

    # ──────────────────────────────────────────────────────────
    # /maintenance on | off
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("maintenance") & filters.user(ADMIN_USERS))
    async def maintenance_cmd(_, message: Message):
        args = message.command
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            return await message.reply(
                "Usage: <code>/maintenance on</code> or <code>/maintenance off</code>",
                parse_mode=enums.ParseMode.HTML,
            )

        mode = args[1].lower() == "on"
        await set_global_setting("MAINTENANCE_MODE", mode)

        if mode:
            await message.reply(
                "🔧 <b>Maintenance mode ON</b>\n\n"
                "✅ Admins can use the bot normally.\n"
                "🚫 All users are blocked until you run <code>/maintenance off</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await message.reply(
                "✅ <b>Maintenance mode OFF</b>\n\n"
                "Bot is back online for all users.",
                parse_mode=enums.ParseMode.HTML,
            )

    # ──────────────────────────────────────────────────────────
    # /missing  —  find missing seasons & episodes per series
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("missing") & filters.user(ADMIN_USERS))
    async def missing_cmd(_, message: Message):
        import asyncio as _asyncio
        from DB.database import get_all_series, sql_db_lock
        from DB.sqlite_db import _get_conn
        from CyberTG.poster import fetch_tmdb, fetch_omdb

        wait_msg = await message.reply("🔍 <b>Scanning for missing content...</b>", parse_mode=enums.ParseMode.HTML)

        # ── Step 1: get all series names from DB ──────────────
        series_list = await get_all_series()   # [(name, file_count), ...]
        if not series_list:
            return await wait_msg.edit_text("❌ No series in database.")

        # ── Step 2: for each series, get what we have in SQLite
        def _get_series_data():
            conn   = _get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT normalized_name, season, episode
                FROM files
                ORDER BY normalized_name, season, episode
            """)
            rows = cursor.fetchall()
            # Build: { name: { season: [ep, ep, ...] } }
            data = {}
            for row in rows:
                name, s, e = row[0], row[1], row[2]
                if name not in data:
                    data[name] = {}
                if s not in data[name]:
                    data[name][s] = []
                if e not in data[name][s]:
                    data[name][s][e] if False else data[name][s].append(e)
            return data

        async with sql_db_lock:
            db_data = await _asyncio.to_thread(_get_series_data)

        # ── Step 3: TMDB lookup for expected episode counts ───
        # Cache to avoid repeat calls for same series
        _tmdb_cache: dict[str, dict] = {}

        async def _get_tmdb_info(name: str) -> dict:
            """Returns {season: total_episodes} from TMDB, or {} if not found."""
            if name in _tmdb_cache:
                return _tmdb_cache[name]

            result = {}
            try:
                import aiohttp
                from config import TMDB_API_KEY

                # Search for the show
                search_url = "https://api.themoviedb.org/3/search/tv"
                async with aiohttp.ClientSession() as session:
                    async with session.get(search_url, params={"api_key": TMDB_API_KEY, "query": name}, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        data = await r.json()

                results = data.get("results", [])
                if not results:
                    _tmdb_cache[name] = result
                    return result

                show_id = results[0]["id"]

                # Get season episode counts
                show_url = f"https://api.themoviedb.org/3/tv/{show_id}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(show_url, params={"api_key": TMDB_API_KEY}, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        show_data = await r.json()

                for season_info in show_data.get("seasons", []):
                    snum  = season_info.get("season_number", 0)
                    count = season_info.get("episode_count", 0)
                    if snum > 0 and count > 0:
                        result[snum] = count

            except Exception:
                pass  # TMDB down or timeout — skip, use DB-only logic

            _tmdb_cache[name] = result
            return result

        # ── Step 4: Detect missing content ───────────────────
        missing_report = []

        for series_name, _ in series_list:
            seasons_data = db_data.get(series_name.lower(), {})
            if not seasons_data:
                continue

            # Skip season 0 (specials/extras)
            real_seasons = sorted(s for s in seasons_data if s > 0)
            if not real_seasons:
                continue

            tmdb_info = await _get_tmdb_info(series_name)

            series_issues = []

            # Check for missing seasons in our range
            if real_seasons:
                min_s, max_s = real_seasons[0], real_seasons[-1]
                missing_seasons = [s for s in range(min_s, max_s + 1) if s not in real_seasons]
                if missing_seasons:
                    series_issues.append(f"  ❌ Missing seasons: {', '.join(f'S{s:02d}' for s in missing_seasons)}")

            # Check for missing episodes within each season
            for season in real_seasons:
                episodes = sorted(set(seasons_data[season]))
                if not episodes:
                    continue

                # Determine expected episode count
                if tmdb_info and season in tmdb_info:
                    expected_count = tmdb_info[season]
                    expected_eps   = list(range(1, expected_count + 1))
                else:
                    # Fallback: assume contiguous from 1 to max we have
                    expected_eps = list(range(1, max(episodes) + 1))

                missing_eps = [e for e in expected_eps if e not in episodes]
                if missing_eps:
                    # Group consecutive missing into ranges for readability
                    def _ranges(nums):
                        nums = sorted(nums)
                        if not nums:
                            return ""
                        ranges, start, end = [], nums[0], nums[0]
                        for n in nums[1:]:
                            if n == end + 1:
                                end = n
                            else:
                                ranges.append(f"EP{start:02d}" if start == end else f"EP{start:02d}-EP{end:02d}")
                                start = end = n
                        ranges.append(f"EP{start:02d}" if start == end else f"EP{start:02d}-EP{end:02d}")
                        return ", ".join(ranges)

                    ep_str = _ranges(missing_eps)
                    src    = "TMDB" if (tmdb_info and season in tmdb_info) else "DB range"
                    series_issues.append(f"  ⚠️ S{season:02d} missing [{src}]: {ep_str}")

            if series_issues:
                missing_report.append(f"📺 {series_name.title()}")
                missing_report.extend(series_issues)
                missing_report.append("")

        # ── Step 5: Send report always as .txt document ───────
        await wait_msg.delete()

        if not missing_report:
            plain = (
                "✅ No Missing Content Found!\n"
                "All series have complete seasons and episodes."
            )
            file_buf      = io.BytesIO(plain.encode("utf-8"))
            file_buf.name = "missing_report.txt"
            return await message.reply_document(
                document=file_buf,
                caption="✅ <b>No missing content found!</b>",
                parse_mode=enums.ParseMode.HTML,
            )

        total_issues = len([l for l in missing_report if l.startswith("📺")])

        header_lines = [
            "=" * 50,
            "       MISSING CONTENT REPORT",
            "=" * 50,
            f"  Checked  : {len(series_list)} series",
            f"  Issues   : {total_issues} series have gaps",
            "=" * 50,
            "",
        ]

        full_text     = "\n".join(header_lines) + "\n".join(missing_report)
        file_buf      = io.BytesIO(full_text.encode("utf-8"))
        file_buf.name = "missing_report.txt"

        await message.reply_document(
            document=file_buf,
            caption=(
                f"📋 <b>Missing Content Report</b>\n"
                f"🔍 Checked : <code>{len(series_list)}</code> series\n"
                f"⚠️ Issues  : <code>{total_issues}</code> series have gaps"
            ),
            parse_mode=enums.ParseMode.HTML,
        )
