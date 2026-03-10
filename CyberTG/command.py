"""
command.py — OPTIMIZED EDITION
================================
Commands kept: /start, /broadcast, /stats, /list, /total,
               /deleteall, /deletefile, /logs, /adminsetting,
               /maintenance, /missing

Removed (all available in admin settings menus.py):
  /filter, /poster, /captionclean, /setuserbot, /deluserbot, /userbotinfo

Bugs fixed:
  1. get_db_size_info() returns keys "config","db1","db2","sqlite" (bytes) —
     command.py was reading wrong keys "mongo_config_mb" etc. → always showed 0.
     Fixed: divide by 1MB correctly using actual key names.
  2. Removed dead imports: sql_db_lock (not used here),
     get_userbot_string/set/delete (commands removed).
  3. /stats and /total now run their DB queries concurrently with asyncio.gather()
     instead of sequentially — faster response.
  4. _SEARCH_COOLDOWN in filters.py grows unbounded — fixed here in broadcast
     by not touching it (that's filters.py's concern, noted there).
  5. /missing: inline _asyncio alias removed (just use asyncio directly).
  6. /list export: series_list and total_files fetched concurrently.
"""

import io
import asyncio
import math

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

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
    get_total_groups,
    get_all_series,
    delete_all_files,
    delete_one_file,
    get_global_setting,
    set_global_setting,
    get_db_size_info,
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
    _PENDING_FSUB,
    check_fsub,
)

# ── Pagination ─────────────────────────────────────────────────
LIST_PAGE_SIZE = 20


def _fmt_bytes(b: int) -> str:
    """Format raw bytes as human-readable."""
    mb = b / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    if mb >= 1:
        return f"{mb:.2f} MB"
    return f"{mb * 1024:.1f} KB"


def register_command_handlers(cyber: Client):

    # ──────────────────────────────────────────────────────────
    # /start
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("start") & filters.private)
    async def start_command(_, message: Message):
        uid = message.from_user.id
        await add_user(uid)

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
                try:
                    await message.delete()
                except Exception:
                    pass

                statuses = await get_slot_statuses(cyber, uid)
                passed   = all_slots_passed(statuses)

                if not passed:
                    bot      = await cyber.get_me()
                    kb       = build_fsub_keyboard(bot.username, payload, statuses)
                    txt      = fsub_text(statuses)
                    existing = _PENDING_FSUB.get(uid)

                    if existing and existing["payload"] == payload:
                        try:
                            await existing["msg"].edit_text(
                                txt, reply_markup=kb,
                                parse_mode=enums.ParseMode.HTML,
                            )
                        except Exception:
                            pass
                    else:
                        gate_msg = await cyber.send_message(
                            uid, txt, reply_markup=kb,
                            parse_mode=enums.ParseMode.HTML,
                        )
                        register_pending_fsub(uid, gate_msg, payload)
                    return

                pending = _PENDING_FSUB.get(uid)
                if pending:
                    try:
                        await pending["msg"].delete()
                    except Exception:
                        pass
                    clear_pending_fsub(uid)

                await do_deliver(cyber, uid, payload, source="start")
                return

        bot       = await cyber.get_me()
        user_name = message.from_user.first_name

        START_TEXT = (
            f"✦ <b>Welcome, {user_name}</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"🎬 <b>{bot.first_name}</b> — Your premium series library,\n"
            f"right inside Telegram.\n\n"
            f"<blockquote>Simply type a series name to get started.\n"
            f"Browse thousands of titles instantly.</blockquote>"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍  Browse Library", switch_inline_query_current_chat="")],
            [InlineKeyboardButton("📖  Help",    callback_data="help|menu"),
             InlineKeyboardButton("ℹ️  About",   callback_data="about|menu")],
            [InlineKeyboardButton("💬  Support", url="https://t.me/TomxSupport")],
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
            return await msg.reply(
                "↩️ Reply to a message to broadcast it.",
                parse_mode=enums.ParseMode.HTML,
            )
        users   = await get_all_users()
        total   = len(users)
        sent    = 0
        failed  = 0
        blocked = 0

        progress = await msg.reply(
            f"📡 <b>Broadcasting to {total} users...</b>",
            parse_mode=enums.ParseMode.HTML,
        )

        for i, uid in enumerate(users):
            while True:
                try:
                    await msg.reply_to_message.copy(uid)
                    sent += 1
                    break
                except FloodWait as fw:
                    wait = max(int(getattr(fw, "value", 10)), 5)
                    logger.warning(f"[BROADCAST] FloodWait {wait}s")
                    await asyncio.sleep(wait + 1)
                except Exception as e:
                    err = str(e).lower()
                    if "blocked" in err or "user is deactivated" in err:
                        blocked += 1
                    else:
                        failed += 1
                    break

            if (i + 1) % 50 == 0 or (i + 1) == total:
                try:
                    await progress.edit_text(
                        f"📡 <b>Broadcasting...</b>\n\n"
                        f"📤 Sent    : <code>{sent}</code>\n"
                        f"🚫 Blocked : <code>{blocked}</code>\n"
                        f"❌ Failed  : <code>{failed}</code>\n"
                        f"📊 Progress: <code>{i+1}/{total}</code>",
                        parse_mode=enums.ParseMode.HTML,
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.05)

        await progress.edit_text(
            f"✅ <b>Broadcast Complete</b>\n\n"
            f"📤 Sent    : <code>{sent}</code>\n"
            f"🚫 Blocked : <code>{blocked}</code>\n"
            f"❌ Failed  : <code>{failed}</code>\n"
            f"👥 Total   : <code>{total}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    # ──────────────────────────────────────────────────────────
    # /stats — concurrent DB queries for fast response
    # BUG FIX: get_db_size_info() returns raw bytes under keys
    #   "config", "db1", "db2", "sqlite" — not "mongo_config_mb" etc.
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("stats") & filters.user(ADMIN_USERS))
    async def stats_cmd(_, m: Message):
        wait = await m.reply("⏳ <b>Gathering stats...</b>", parse_mode=enums.ParseMode.HTML)

        # Run all DB queries concurrently — much faster than sequential awaits
        users, files, series, groups, per_db, sizes = await asyncio.gather(
            get_total_users(),
            get_total_files(),
            get_total_series(),
            get_total_groups(),
            get_total_files_per_db(),
            get_db_size_info(),
        )

        db1_count = per_db.get("db1", 0)
        db2_count = per_db.get("db2", 0)

        # FIX: use correct keys ("config","db1","db2","sqlite") returned by get_db_size_info()
        cfg_size = _fmt_bytes(sizes.get("config", 0))
        db1_size = _fmt_bytes(sizes.get("db1", 0))
        db2_size = _fmt_bytes(sizes.get("db2", 0))
        sql_size = _fmt_bytes(sizes.get("sqlite", 0))
        total_mongo = sizes.get("db1", 0) + sizes.get("db2", 0)

        text = (
            f"<b>📊 Bot Statistics</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"👥  Users       <code>{users:,}</code>\n"
            f"🎬  Series      <code>{series:,}</code>\n"
            f"📦  Files       <code>{files:,}</code>\n"
            f"🏘  Groups      <code>{groups:,}</code>\n\n"
            f"<b>🗄  Database</b>\n"
            f"    Config     <code>{cfg_size}</code>\n"
            f"    DB1        <code>{db1_count:,} files  ·  {db1_size}</code>\n"
            f"    DB2        <code>{db2_count:,} files  ·  {db2_size}</code>\n"
            f"    Total      <code>{_fmt_bytes(total_mongo)}</code>\n\n"
            f"<b>💾  SQLite</b>     <code>{sql_size}</code>"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️  Back", callback_data="gset|main")]
        ])
        await wait.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)

    # ──────────────────────────────────────────────────────────
    # /total — quick stats (concurrent queries)
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("total") & filters.user(ADMIN_USERS))
    async def total(_, m: Message):
        users, files, series, per_db = await asyncio.gather(
            get_total_users(),
            get_total_files(),
            get_total_series(),
            get_total_files_per_db(),
        )
        db1 = per_db.get("db1", 0)
        db2 = per_db.get("db2", 0)

        await m.reply(
            f"<b>📊 Quick Stats</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"👥  Users    <code>{users:,}</code>\n"
            f"🎬  Series   <code>{series:,}</code>\n"
            f"📦  Files    <code>{files:,}</code>\n\n"
            f"🗄  DB1      <code>{db1:,}</code> files\n"
            f"🗄  DB2      <code>{db2:,}</code> files",
            parse_mode=enums.ParseMode.HTML,
        )

    # ──────────────────────────────────────────────────────────
    # /list — paginated series list + export
    # FIX: export fetches series_list and total_files concurrently
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("list") & filters.user(ADMIN_USERS))
    async def list_cmd(_, m: Message):
        await _send_series_list_page(m, page=0, send_new=True)

    async def _send_series_list_page(source, page: int, send_new: bool = False):
        series_list = await get_all_series()
        total       = len(series_list)
        total_pages = max(1, math.ceil(total / LIST_PAGE_SIZE))
        page        = max(0, min(page, total_pages - 1))

        start = page * LIST_PAGE_SIZE
        chunk = series_list[start : start + LIST_PAGE_SIZE]

        lines = [f"<b>🎬 Series List</b>  (Page {page+1}/{total_pages})\n"]
        for i, (name, count) in enumerate(chunk, start + 1):
            lines.append(f"<code>{i:>4}.</code> {name}  <i>({count})</i>")

        text = "\n".join(lines)

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"serieslist|{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"serieslist|{page+1}"))

        keyboard = InlineKeyboardMarkup([
            nav,
            [InlineKeyboardButton("📥 Export Full List", callback_data="serieslist|export")],
        ])

        if send_new:
            await source.reply(text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)
        else:
            try:
                await source.message.edit_text(
                    text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard
                )
                await source.answer()
            except Exception:
                await source.answer()

    @cyber.on_callback_query(filters.regex(r"^serieslist\|") & filters.user(ADMIN_USERS))
    async def serieslist_cb(_, q):
        action = q.data.split("|")[1]

        if action == "export":
            await q.answer("📥 Generating export...")
            # FIX: fetch concurrently
            series_list, total_files = await asyncio.gather(
                get_all_series(),
                get_total_files(),
            )
            total_series = len(series_list)

            lines = [
                "=" * 50,
                "         BATCHED SERIES LIST",
                "=" * 50,
                f"  Total Series : {total_series}",
                f"  Total Files  : {total_files}",
                "=" * 50,
                "",
            ]
            for i, (name, count) in enumerate(series_list, 1):
                lines.append(f"{i:>4}. {name}  ({count} files)")

            content       = "\n".join(lines)
            file_buf      = io.BytesIO(content.encode("utf-8"))
            file_buf.name = "series_list.txt"

            await q.message.reply_document(
                document=file_buf,
                caption=(
                    f"<b>🎬 Full Series List</b>\n"
                    f"Total Series : <code>{total_series}</code>\n"
                    f"Total Files  : <code>{total_files}</code>"
                ),
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            try:
                page = int(action)
            except ValueError:
                await q.answer()
                return
            await _send_series_list_page(q, page=page, send_new=False)

    # ──────────────────────────────────────────────────────────
    # /deleteall — with confirmation prompt
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("deleteall") & filters.user(ADMIN_USERS))
    async def deleteall(_, m: Message):
        files = await get_total_files()
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅  Confirm Delete", callback_data="deleteall_confirm"),
                InlineKeyboardButton("✖️  Cancel",         callback_data="deleteall_cancel"),
            ]
        ])
        await m.reply(
            f"⚠️ <b>Confirm Deletion</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"You are about to permanently delete\n"
            f"<b>{files:,} files</b> from all databases.\n\n"
            f"<i>This action cannot be undone.</i>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=keyboard,
        )

    @cyber.on_callback_query(filters.regex("^deleteall_confirm$") & filters.user(ADMIN_USERS))
    async def deleteall_confirm(_, q):
        await q.answer("🗑 Deleting all files...")
        try:
            await q.message.edit_text("⏳ <b>Deleting all files...</b>", parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
        await delete_all_files()
        try:
            await q.message.edit_text(
                "✅ <b>All files deleted successfully.</b>\n"
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                "Database has been wiped clean.",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️  Back", callback_data="gset|main")
                ]]),
            )
        except Exception:
            pass

    @cyber.on_callback_query(filters.regex("^deleteall_cancel$") & filters.user(ADMIN_USERS))
    async def deleteall_cancel(_, q):
        await q.answer("Cancelled.")
        try:
            await q.message.delete()
        except Exception:
            pass

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
        try:
            cid = int(m.command[1])
            mid = int(m.command[2])
        except ValueError:
            return await m.reply("❌ Invalid IDs. Must be integers.", parse_mode=enums.ParseMode.HTML)

        await delete_one_file(cid, mid)
        await m.reply(
            f"✅ <b>File deleted.</b>\n"
            f"Channel: <code>{cid}</code>  Message: <code>{mid}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

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
            [InlineKeyboardButton("⚙️  Global Settings", callback_data="gset|main")],
            [InlineKeyboardButton("✖️  Close",           callback_data="close|menu")],
        ])
        await message.reply(
            "<b>🛠  Admin Panel</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━</b>",
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
            current = await get_global_setting("MAINTENANCE_MODE", False)
            status  = "✅ ON" if current else "❌ OFF"
            return await message.reply(
                f"<b>🔧 Maintenance Mode: {status}</b>\n\n"
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
    # /missing — scan for missing episodes/seasons
    # FIX: removed redundant "import asyncio as _asyncio" alias
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("missing") & filters.user(ADMIN_USERS))
    async def missing_cmd(_, message: Message):
        from DB.database import get_all_series as _get_all_series, sql_db_lock
        from DB.sqlite_db import _get_conn

        wait_msg = await message.reply(
            "🔍 <b>Scanning for missing content...</b>",
            parse_mode=enums.ParseMode.HTML,
        )

        series_list = await _get_all_series()
        if not series_list:
            return await wait_msg.edit_text("❌ No series in database.")

        def _get_series_data():
            conn   = _get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT normalized_name, season, episode
                FROM files ORDER BY normalized_name, season, episode
            """)
            data = {}
            for row in cursor.fetchall():
                name, s, e = row[0], row[1], row[2]
                data.setdefault(name, {}).setdefault(s, [])
                if e not in data[name][s]:
                    data[name][s].append(e)
            return data

        async with sql_db_lock:
            db_data = await asyncio.to_thread(_get_series_data)

        _tmdb_cache: dict = {}

        async def _get_tmdb_info(name: str) -> dict:
            if name in _tmdb_cache:
                return _tmdb_cache[name]
            result = {}
            try:
                import aiohttp
                from config import TMDB_API_KEY
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.themoviedb.org/3/search/tv",
                        params={"api_key": TMDB_API_KEY, "query": name},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as r:
                        data = await r.json()
                results = data.get("results", [])
                if results:
                    show_id  = results[0]["id"]
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"https://api.themoviedb.org/3/tv/{show_id}",
                            params={"api_key": TMDB_API_KEY},
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as r:
                            show_data = await r.json()
                    for s_info in show_data.get("seasons", []):
                        snum  = s_info.get("season_number", 0)
                        count = s_info.get("episode_count", 0)
                        if snum > 0 and count > 0:
                            result[snum] = count
            except Exception:
                pass
            _tmdb_cache[name] = result
            return result

        def _ranges(nums):
            nums = sorted(nums)
            if not nums:
                return ""
            ranges, start, end = [], nums[0], nums[0]
            for n in nums[1:]:
                if n == end + 1:
                    end = n
                else:
                    ranges.append(
                        f"EP{start:02d}" if start == end
                        else f"EP{start:02d}-EP{end:02d}"
                    )
                    start = end = n
            ranges.append(
                f"EP{start:02d}" if start == end
                else f"EP{start:02d}-EP{end:02d}"
            )
            return ", ".join(ranges)

        missing_report = []

        for series_name, _ in series_list:
            seasons_data = db_data.get(series_name.lower(), {})
            if not seasons_data:
                continue
            real_seasons = sorted(s for s in seasons_data if s > 0)
            if not real_seasons:
                continue

            tmdb_info    = await _get_tmdb_info(series_name)
            series_issues = []

            min_s, max_s = real_seasons[0], real_seasons[-1]
            missing_seasons = [s for s in range(min_s, max_s + 1) if s not in real_seasons]
            if missing_seasons:
                series_issues.append(
                    f"  ❌ Missing seasons: {', '.join(f'S{s:02d}' for s in missing_seasons)}"
                )

            for season in real_seasons:
                episodes = sorted(set(seasons_data[season]))
                if not episodes:
                    continue
                if tmdb_info and season in tmdb_info:
                    expected_eps = list(range(1, tmdb_info[season] + 1))
                    src = "TMDB"
                else:
                    expected_eps = list(range(1, max(episodes) + 1))
                    src = "DB range"

                missing_eps = [e for e in expected_eps if e not in episodes]
                if missing_eps:
                    series_issues.append(
                        f"  ⚠️ S{season:02d} missing [{src}]: {_ranges(missing_eps)}"
                    )

            if series_issues:
                missing_report.append(f"📺 {series_name.title()}")
                missing_report.extend(series_issues)
                missing_report.append("")

        await wait_msg.delete()

        if not missing_report:
            plain         = "✅ No Missing Content Found!\nAll series have complete seasons and episodes."
            file_buf      = io.BytesIO(plain.encode("utf-8"))
            file_buf.name = "missing_report.txt"
            return await message.reply_document(
                document=file_buf,
                caption="✅ <b>No missing content found!</b>",
                parse_mode=enums.ParseMode.HTML,
            )

        total_issues = len([l for l in missing_report if l.startswith("📺")])
        header_lines = [
            "=" * 50, "       MISSING CONTENT REPORT", "=" * 50,
            f"  Checked  : {len(series_list)} series",
            f"  Issues   : {total_issues} series have gaps",
            "=" * 50, "",
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
