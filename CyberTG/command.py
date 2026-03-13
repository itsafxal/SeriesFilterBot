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
        users, files, series, groups, per_db = await asyncio.gather(
            get_total_users(),
            get_total_files(),
            get_total_series(),
            get_total_groups(),
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
            f"🔖  Group    <code>{groups:,}</code>\n\n"
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
    # /missing — full per-series, per-language, per-quality report
    # ──────────────────────────────────────────────────────────
    @cyber.on_message(filters.command("missing") & filters.user(ADMIN_USERS))
    async def missing_cmd(_, message: Message):
        import aiohttp
        from DB.database import get_all_series as _get_all_series, sql_db_lock
        from DB.sqlite_db import _get_conn
        from config import TMDB_API_KEY

        # ── Language flags & display order ────────────────────
        LANG_FLAG = {
            "Hindi":     "🇮🇳 Hindi",
            "Tamil":     "🇮🇳 Tamil",
            "Telugu":    "🇮🇳 Telugu",
            "Malayalam": "🇮🇳 Malayalam",
            "Kannada":   "🇮🇳 Kannada",
            "Bengali":   "🇮🇳 Bengali",
            "English":   "🇺🇸 English",
            "Korean":    "🇰🇷 Korean",
            "Japanese":  "🇯🇵 Japanese",
            "Chinese":   "🇨🇳 Chinese",
            "Spanish":   "🇪🇸 Spanish",
            "French":    "🇫🇷 French",
            "German":    "🇩🇪 German",
            "Arabic":    "🇸🇦 Arabic",
            "Turkish":   "🇹🇷 Turkish",
            "Italian":   "🇮🇹 Italian",
            "Portuguese":"🇧🇷 Portuguese",
            "Russian":   "🇷🇺 Russian",
            "Thai":      "🇹🇭 Thai",
        }
        LANG_ORDER = list(LANG_FLAG.keys())

        wait_msg = await message.reply(
            "🔍 <b>Scanning for missing content...</b>",
            parse_mode=enums.ParseMode.HTML,
        )

        series_list = await _get_all_series()
        if not series_list:
            await wait_msg.delete()
            return await message.reply("❌ No series in database.")

        # ── Single SQL fetch — all data in one pass ───────────
        def _get_all_db_data():
            """
            Returns dict:
              {
                normalized_name: {
                  "overall":  {season: {episode: True}},
                  "by_lang":  {lang:  {season: {episode: True}}},
                  "by_qual":  {qual:  {season: {episode: True}}},
                  "by_lang_qual": {lang: {qual: {season: {episode: True}}}},
                }
              }
            Fetches ALL columns in one query — no N+1 queries.
            """
            conn   = _get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT normalized_name, season, episode, language, quality
                FROM files
                WHERE season > 0 AND episode > 0
                ORDER BY normalized_name, season, episode
            """)

            data: dict = {}
            for row in cursor.fetchall():
                name    = row[0]
                season  = row[1]
                episode = row[2]
                lang    = row[3] or "Unknown"
                qual    = row[4] or "Unknown"

                if name not in data:
                    data[name] = {
                        "overall":      {},
                        "by_lang":      {},
                        "by_qual":      {},
                        "by_lang_qual": {},
                    }

                d = data[name]

                # overall
                d["overall"].setdefault(season, set()).add(episode)

                # by language
                d["by_lang"].setdefault(lang, {}).setdefault(season, set()).add(episode)

                # by quality
                d["by_qual"].setdefault(qual, {}).setdefault(season, set()).add(episode)

                # by lang+quality combined
                d["by_lang_qual"].setdefault(lang, {}).setdefault(qual, {}).setdefault(season, set()).add(episode)

            return data

        async with sql_db_lock:
            db_data = await asyncio.to_thread(_get_all_db_data)

        # ── TMDB fetch with caching ───────────────────────────
        _tmdb_cache: dict = {}

        async def _get_tmdb_info(name: str) -> dict:
            """Returns {season_num: episode_count, ..., "_total_seasons": N, "_name": str}"""
            if name in _tmdb_cache:
                return _tmdb_cache[name]
            result = {}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.themoviedb.org/3/search/tv",
                        params={"api_key": TMDB_API_KEY(), "query": name},
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as r:
                        search_data = await r.json()

                hits = search_data.get("results", [])
                if not hits:
                    _tmdb_cache[name] = result
                    return result

                show_id   = hits[0]["id"]
                tmdb_name = hits[0].get("name", name)

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://api.themoviedb.org/3/tv/{show_id}",
                        params={"api_key": TMDB_API_KEY()},
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as r:
                        show_data = await r.json()

                real_seasons = 0
                for s_info in show_data.get("seasons", []):
                    snum  = s_info.get("season_number", 0)
                    count = s_info.get("episode_count", 0)
                    if snum > 0 and count > 0:
                        result[snum] = count
                        real_seasons += 1

                result["_total_seasons"] = real_seasons
                result["_name"]          = tmdb_name

            except Exception:
                pass
            _tmdb_cache[name] = result
            return result

        # ── Helpers ───────────────────────────────────────────

        def _fmt_episodes(ep_set: set) -> str:
            """Format a set of episode numbers as compact ranges: E01, E03-E05, E08"""
            nums = sorted(ep_set)
            if not nums:
                return "—"
            parts, start, end = [], nums[0], nums[0]
            for n in nums[1:]:
                if n == end + 1:
                    end = n
                else:
                    parts.append(f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}")
                    start = end = n
            parts.append(f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}")
            return ", ".join(parts)

        def _missing_eps_for_season(have: set, expected_count: int | None) -> set:
            """Return set of missing episode numbers for a season."""
            if not have:
                return set()
            if expected_count:
                return set(range(1, expected_count + 1)) - have
            # fallback: gaps within the range we have
            return set(range(1, max(have) + 1)) - have

        def _build_series_block(
            series_name: str,
            series_data: dict,
            tmdb_info: dict,
        ) -> list[str]:
            """
            Build the full text block for one series.
            Returns list of lines.
            """
            overall     = series_data["overall"]      # {season: set(episodes)}
            by_lang     = series_data["by_lang"]      # {lang: {season: set}}
            by_lang_qual= series_data["by_lang_qual"] # {lang: {qual: {season: set}}}

            all_seasons_in_db = sorted(s for s in overall if s > 0)
            if not all_seasons_in_db:
                return []

            tmdb_total = tmdb_info.get("_total_seasons", 0)
            tmdb_name  = tmdb_info.get("_name", series_name.title())

            # ── Overall missing seasons (vs TMDB) ─────────────
            if tmdb_total:
                all_expected_seasons = set(range(1, tmdb_total + 1))
            else:
                # fallback: contiguous range from min to max we have
                all_expected_seasons = set(range(min(all_seasons_in_db), max(all_seasons_in_db) + 1))

            have_seasons    = set(all_seasons_in_db)
            missing_seasons = sorted(all_expected_seasons - have_seasons)

            # ── Overall missing episodes (across all seasons we have) ──
            all_missing_eps: dict[int, set] = {}   # {season: missing_ep_set}
            for s in all_seasons_in_db:
                exp  = tmdb_info.get(s)   # may be None
                miss = _missing_eps_for_season(overall[s], exp)
                if miss:
                    all_missing_eps[s] = miss

            # Check if anything is actually missing
            has_missing_seasons = bool(missing_seasons)
            has_missing_eps     = bool(all_missing_eps)
            if not has_missing_seasons and not has_missing_eps:
                # Check per-lang too — maybe overall is fine but some lang is incomplete
                lang_has_issues = False
                for lang, lang_seasons in by_lang.items():
                    lang_have = set(lang_seasons.keys())
                    if lang_have != have_seasons:
                        lang_has_issues = True
                        break
                    for s, ep_set in lang_seasons.items():
                        exp  = tmdb_info.get(s)
                        miss = _missing_eps_for_season(ep_set, exp)
                        if miss:
                            lang_has_issues = True
                            break
                    if lang_has_issues:
                        break
                if not lang_has_issues:
                    return []   # fully complete — skip

            lines = []
            B = "╔══════════════════════════════╗"
            M = "╚══════════════════════════════╝"
            D = "━━━━━━━━━━━━━━━━━━━━━━"

            lines.append(B)
            lines.append("  📺 SERIES STATUS REPORT")
            lines.append(M)
            lines.append(f"🎬 Series Name       : {tmdb_name}")
            if tmdb_total:
                lines.append(f"📦 Total Seasons (TMDB): {tmdb_total}")
            else:
                lines.append(f"📦 Seasons in DB     : {len(all_seasons_in_db)}")
            lines.append(D)
            lines.append("❌ MISSING CONTENT")
            lines.append(D)

            # Missing seasons
            if missing_seasons:
                lines.append(f"📁 Seasons Missing :")
                lines.append("➤ " + ", ".join(f"S{s:02d}" for s in missing_seasons))
            else:
                lines.append("📁 Seasons Missing : None ✅")

            # Missing episodes overall
            if all_missing_eps:
                lines.append("🎞 Episodes Missing :")
                for s in sorted(all_missing_eps):
                    miss = all_missing_eps[s]
                    lines.append(f"  S{s:02d} ➤ {_fmt_episodes(miss)}")
            else:
                lines.append("🎞 Episodes Missing : None ✅")

            # ── Language Status ────────────────────────────────
            lines.append(D)
            lines.append("🌍 LANGUAGE STATUS")
            lines.append(D)

            # Sort langs: known first in LANG_ORDER, then others
            all_langs = sorted(
                by_lang.keys(),
                key=lambda l: (LANG_ORDER.index(l) if l in LANG_ORDER else 999, l)
            )

            for lang in all_langs:
                lang_seasons  = by_lang[lang]
                lang_have     = set(lang_seasons.keys())
                lang_miss_s   = sorted(have_seasons - lang_have)  # seasons others have but this lang doesn't
                lang_miss_eps : dict[int, set] = {}

                for s in sorted(lang_seasons.keys()):
                    exp  = tmdb_info.get(s)
                    miss = _missing_eps_for_season(lang_seasons[s], exp)
                    if miss:
                        lang_miss_eps[s] = miss

                flag_label = LANG_FLAG.get(lang, f"🌐 {lang}")
                lines.append(f"{flag_label}")

                if lang_miss_s:
                    lines.append(f"  • Missing Seasons : " + ", ".join(f"S{s:02d}" for s in lang_miss_s))
                else:
                    lines.append(f"  • Seasons : S" + ", S".join(f"{s:02d}" for s in sorted(lang_have)))

                if lang_miss_eps:
                    for s in sorted(lang_miss_eps):
                        lines.append(f"  • S{s:02d} Missing Eps : {_fmt_episodes(lang_miss_eps[s])}")
                else:
                    lines.append(f"  • Episodes : Complete ✅")

                # ── Per quality breakdown under this lang ──────
                if lang in by_lang_qual:
                    for qual in sorted(by_lang_qual[lang].keys()):
                        qual_seasons = by_lang_qual[lang][qual]
                        q_have       = set(qual_seasons.keys())
                        q_miss_s     = sorted(lang_have - q_have)
                        q_miss_eps   : dict[int, set] = {}
                        for s in sorted(qual_seasons.keys()):
                            exp  = tmdb_info.get(s)
                            miss = _missing_eps_for_season(qual_seasons[s], exp)
                            if miss:
                                q_miss_eps[s] = miss

                        # Only show quality row if it has issues
                        if q_miss_s or q_miss_eps:
                            lines.append(f"  📼 [{qual}]")
                            if q_miss_s:
                                lines.append("    ↳ Missing Seasons : " + ", ".join(f"S{s:02d}" for s in q_miss_s))
                            if q_miss_eps:
                                for s in sorted(q_miss_eps):
                                    lines.append(f"    ↳ S{s:02d} Missing Eps : {_fmt_episodes(q_miss_eps[s])}")

            lines.append(D)
            lines.append("✅ Report End")
            lines.append(D)
            lines.append("")
            return lines

        # ── Main loop — build report ──────────────────────────
        all_blocks : list[str] = []
        series_with_issues     = 0
        processed              = 0

        for series_name, _ in series_list:
            processed += 1
            # Progress update every 25 series
            if processed % 25 == 0:
                try:
                    await wait_msg.edit_text(
                        f"🔍 <b>Scanning...</b> <code>{processed}/{len(series_list)}</code>",
                        parse_mode=enums.ParseMode.HTML,
                    )
                except Exception:
                    pass

            series_data = db_data.get(series_name, {})
            if not series_data or not series_data.get("overall"):
                # try case-insensitive match
                key = next((k for k in db_data if k.lower() == series_name.lower()), None)
                if key:
                    series_data = db_data[key]
                else:
                    continue

            tmdb_info = await _get_tmdb_info(series_name)

            block = _build_series_block(series_name, series_data, tmdb_info)
            if block:
                all_blocks.extend(block)
                series_with_issues += 1

        await wait_msg.delete()

        # ── Output ────────────────────────────────────────────
        total_checked = len(series_list)

        if not all_blocks:
            plain = (
                "✅ NO MISSING CONTENT\n"
                + "=" * 50 + "\n"
                + f"  Checked : {total_checked} series\n"
                + "  Result  : All series complete!\n"
                + "=" * 50
            )
            file_buf      = io.BytesIO(plain.encode("utf-8"))
            file_buf.name = "missing_report.txt"
            return await message.reply_document(
                document=file_buf,
                caption="✅ <b>No missing content found!</b>",
                parse_mode=enums.ParseMode.HTML,
            )

        header = [
            "=" * 50,
            "      MISSING CONTENT REPORT",
            "=" * 50,
            f"  Checked : {total_checked} series",
            f"  Issues  : {series_with_issues} series have gaps",
            "=" * 50,
            "",
        ]
        full_text     = "\n".join(header) + "\n".join(all_blocks)
        file_buf      = io.BytesIO(full_text.encode("utf-8"))
        file_buf.name = "missing_report.txt"
        await message.reply_document(
            document=file_buf,
            caption=(
                f"📋 <b>Missing Content Report</b>\n"
                f"🔍 Checked : <code>{total_checked}</code> series\n"
                f"⚠️ Issues  : <code>{series_with_issues}</code> series have gaps"
            ),
            parse_mode=enums.ParseMode.HTML,
        )
