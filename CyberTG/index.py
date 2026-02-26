"""
index.py  —  DUAL-MODE INDEXER  (User Session + Bot fallback)
===============================================================

USER MODE  (session string stored in DB)
─────────────────────────────────────────
  Client   : Pyrogram user account (StringSession)
  API call : client.get_chat_history()   →  Telegram GetHistory
  Batch    : 100 messages per call
  Delay    : 0.05s between calls  (Telegram allows much more here)
  Speed    : ~1,000–3,000 messages/min  (10-50× faster than bot)
  FloodWait: Nearly zero  (GetHistory has a generous rate limit)
  Gaps     : None  (history is sequential, no empty IDs)
  Lifecycle: Client STARTED at index begin → TERMINATED at index end

BOT MODE  (no session string)
──────────────────────────────
  Client   : Bot token (existing bot client)
  API call : bot.get_messages(id_list)   →  Telegram GetMessages
  Batch    : 200 IDs per call
  Delay    : 0.5s mandatory between calls  (prevents FloodWait)
  Speed    : ~350–400 IDs/s
  FloodWait: Retried with sleep → same batch retried
  Gaps     : Skipped IDs counted as skipped (normal for channels)
"""

import asyncio
import gc
import time
from typing import Optional

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChannelPrivate
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import logger, ADMIN_USERS
from DB.database import (
    get_bot_state, save_bot_state,
    get_global_setting, set_global_setting,
    delete_all_index_states, get_index_channels,
)
from utils.cyber_index import (
    parse_message_to_doc, batch_save_file_docs,
    save_series_file_for_bulk_index,
    set_caption_lang_cache, batch_dedup_check,
)
from utils.cyber_filter import extract_links
from utils.session import get_index_client, terminate_index_client, has_session_string

# ─────────────────────────────────────────────────────────────
# TUNING
# ─────────────────────────────────────────────────────────────

# User (session) mode
USER_BATCH        = 100    # messages per get_chat_history() call
USER_CALL_DELAY   = 0.05   # 50 ms between history calls

# Bot mode
BOT_BATCH         = 200    # IDs per get_messages() call
BOT_CALL_DELAY    = 0.5    # 500 ms between calls (prevents FloodWait)

# Shared
SAVE_BATCH_SIZE     = 500  # buffer size before DB flush
CHECKPOINT_EVERY    = 10   # save checkpoint every N bot-batches
PROGRESS_EVERY      = 30   # seconds between Telegram message edits

INDEX_SKIP_KEY    = "INDEX_SKIP_ID"
INDEX_STATUS_KEY  = "CURRENT_INDEX_TASK"

media_filter = filters.document | filters.video | filters.audio
index_task: Optional[asyncio.Task] = None

# ─────────────────────────────────────────────────────────────
# LOCK — used by main.py to check if an index is running
# ─────────────────────────────────────────────────────────────
lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────
# TELEGRAM WRAPPERS
# ─────────────────────────────────────────────────────────────

async def _fw_sleep(exc: FloodWait):
    wait = max(int(getattr(exc, "value", 10)), 5)
    logger.warning(f"[INDEX] FloodWait {wait}s")
    await asyncio.sleep(wait + 2)


async def safe_send(bot: Client, uid: int, text: str, **kw) -> Optional[Message]:
    while True:
        try:
            return await bot.send_message(uid, text, **kw)
        except FloodWait as f:
            await _fw_sleep(f)
        except Exception as e:
            logger.error(f"[INDEX] safe_send: {e}")
            return None


_last_text: dict[int, str] = {}


async def safe_edit(msg: Message, text: str, **kw):
    if msg is None or _last_text.get(msg.id) == text:
        return
    try:
        await msg.edit_text(text, **kw)
        _last_text[msg.id] = text
    except FloodWait as f:
        await _fw_sleep(f)
        try:
            await msg.edit_text(text, **kw)
            _last_text[msg.id] = text
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[INDEX] safe_edit: {e}")


def _cancel_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ Cancel Indexing", callback_data="cancel_index")
    ]])


# ─────────────────────────────────────────────────────────────
# PROGRESS
# ─────────────────────────────────────────────────────────────

def _progress(
    *, cid, start_id, last_id,
    checked, saved, skipped, dup,
    calls, fw_count, mode, mps, status="⚡ Indexing...",
) -> str:
    span   = max(last_id - start_id, 1)
    pct    = min((cid - start_id) / span * 100, 100.0)
    bar    = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    remain = max(last_id - cid, 0)
    eta    = ""
    if mps > 0 and remain:
        s = int(remain / mps)
        eta = f"  ETA ~{s}s" if s < 60 else f"  ETA ~{s//60}m" if s < 3600 else f"  ETA ~{s//3600}h{s%3600//60}m"

    icon = "👤 User" if mode == "user" else "🤖 Bot"
    lines = [
        f"{status}  {icon}",
        "",
        f"[{bar}] **{pct:.1f}%**{eta}",
        f"ID `{cid}` / `{last_id}`",
        "",
        f"📦 **Checked**   : `{checked:,}`  (`{calls}` calls)",
        f"✅ **Saved**     : `{saved:,}`",
        f"⏭ **Skipped**   : `{skipped:,}`",
        f"🔁 **Duplicate** : `{dup:,}`",
    ]
    if fw_count:
        lines.append(f"⏸ **FloodWait**  : `{fw_count}` times")
    lines += ["", f"⚡ **Speed**  : `{mps:.0f}` msg/s", f"🎯 Mode: `{icon}`"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# FETCH HELPERS
# ─────────────────────────────────────────────────────────────

async def _bot_fetch(bot, chat_id, id_window, fw_count):
    """Get messages by ID list — bot mode."""
    while True:
        try:
            msgs = await bot.get_messages(chat_id, id_window)
            return msgs if isinstance(msgs, list) else [msgs]
        except FloodWait as fw:
            fw_count[0] += 1
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[BOT] FloodWait {wait}s on get_messages — retrying")
            await asyncio.sleep(wait + 1)
        except ChannelPrivate:
            logger.error(f"[BOT] Channel {chat_id} is private")
            return []
        except Exception as e:
            logger.error(f"[BOT] get_messages error: {e}")
            await asyncio.sleep(3)
            return []


async def _user_history(client, chat_id, start_id, last_id, fw_count):
    """
    AsyncGenerator: yields messages from [start_id..last_id] using GetHistory.
    GetHistory is newest-first, so we start at last_id+1 and walk backwards.
    """
    offset_id = last_id + 1
    while True:
        batch = []
        stop  = False
        try:
            async for msg in client.get_chat_history(
                chat_id, limit=USER_BATCH, offset_id=offset_id
            ):
                if msg.id < start_id:
                    stop = True
                    break
                batch.append(msg)
                offset_id = msg.id   # walk backwards
        except FloodWait as fw:
            fw_count[0] += 1
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[USER] FloodWait {wait}s on get_chat_history — retrying")
            await asyncio.sleep(wait + 1)
            continue
        except ChannelPrivate:
            logger.error(f"[USER] Channel {chat_id} is private")
            return
        except Exception as e:
            logger.error(f"[USER] get_chat_history error: {e}")
            await asyncio.sleep(2)
            return

        for msg in batch:
            yield msg

        if stop or not batch:
            return

        await asyncio.sleep(USER_CALL_DELAY)


# ─────────────────────────────────────────────────────────────
# BUFFER FLUSH
# ─────────────────────────────────────────────────────────────

async def _flush(buf: list, target: str) -> tuple[int, int]:
    if not buf:
        return 0, 0
    to_save, n_dup = await batch_dedup_check(buf)
    saved = 0
    if to_save:
        saved = await batch_save_file_docs(to_save, target=target)
        for doc in to_save:
            logger.info(
                f"[IDX] ✅ {doc['normalized_name']!r} "
                f"S{doc['season']:02d}E{doc['episode']:02d} "
                f"{doc['quality']} {doc['language']}"
            )
    return saved, n_dup


# ─────────────────────────────────────────────────────────────
# CORE TASK
# ─────────────────────────────────────────────────────────────

async def index_files_task(
    bot: Client, status_msg: Message,
    chat_id: int, start_id: int, last_id: int, target: str = "both",
):
    global index_task

    chat_key     = f"index_{chat_id}"
    target_label = {"db1": "DB1", "db2": "DB2", "both": "DB1+DB2"}.get(target, target)

    # Resume / skip checkpoint
    last_cp  = await get_bot_state(chat_key)
    skip_raw = await get_global_setting(INDEX_SKIP_KEY, 0)
    try:
        skip_id = int(skip_raw) if skip_raw else 0
    except Exception:
        skip_id = 0

    candidates   = [start_id, skip_id + 1]
    if last_cp:
        try: candidates.append(int(last_cp))
        except Exception: pass
    start_from   = max(candidates)

    await set_global_setting(INDEX_STATUS_KEY, f"RUNNING|{chat_id}|{start_id}|{last_id}|{target}")

    # Cache CAPTION_LANG for the run  (zero per-doc MongoDB calls)
    cl_flag = await get_global_setting("CAPTION_LANG", True)
    set_caption_lang_cache(bool(cl_flag))

    # Start user client if session is stored
    user_client, mode = await get_index_client()

    logger.info(f"[INDEX] mode={mode.upper()} chat={chat_id} {start_from}→{last_id} target={target}")

    # Counters
    checked = saved = skipped = dup = calls = 0
    fw_count   = [0]
    t0         = time.monotonic()
    last_edit  = [0.0]
    cur_id     = [start_from]

    def mps():
        return checked / max(time.monotonic() - t0, 0.001)

    async def push(force=False, status="⚡ Indexing..."):
        now = time.monotonic()
        if not force and now - last_edit[0] < PROGRESS_EVERY:
            return
        await safe_edit(
            status_msg,
            _progress(
                cid=cur_id[0], start_id=start_id, last_id=last_id,
                checked=checked, saved=saved, skipped=skipped, dup=dup,
                calls=calls, fw_count=fw_count[0], mode=mode,
                mps=mps(), status=status,
            ),
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        last_edit[0] = time.monotonic()

    await push(force=True)

    buf   = []
    cp_n  = 0   # batches since last checkpoint (bot mode)

    async def _proc(msg) -> Optional[dict]:
        if msg is None or msg.empty:
            return None
        if not (msg.document or msg.video or msg.audio):
            return None
        return await parse_message_to_doc(msg)

    async with lock:
        try:
            # ══════════════════════════════════════════════════════
            # USER MODE  —  GetHistory  (fast, nearly no FloodWait)
            # ══════════════════════════════════════════════════════
            if mode == "user":
                async for msg in _user_history(
                    user_client, chat_id, start_from, last_id, fw_count
                ):
                    cur_id[0]  = msg.id
                    checked   += 1
                    calls     += 1

                    doc = await _proc(msg)
                    if doc:
                        buf.append(doc)
                    else:
                        skipped += 1

                    # Flush when buffer full
                    if len(buf) >= SAVE_BATCH_SIZE:
                        sv, nd = await _flush(list(buf), target)
                        saved += sv; dup += nd
                        buf.clear()

                    # Lightweight checkpoint (every ~1000 messages)
                    cp_n += 1
                    if cp_n >= 1000:
                        await save_bot_state(chat_key, cur_id[0])
                        cp_n = 0

                    await push()

                # Final flush
                if buf:
                    sv, nd = await _flush(list(buf), target)
                    saved += sv; dup += nd
                    buf.clear()

            # ══════════════════════════════════════════════════════
            # BOT MODE  —  GetMessages  (reliable, slower)
            # ══════════════════════════════════════════════════════
            else:
                current     = start_from
                total_span  = max(last_id - start_id, 1)

                while current <= last_id:
                    end        = min(current + BOT_BATCH - 1, last_id)
                    id_window  = list(range(current, end + 1))
                    is_last    = end >= last_id
                    flush_now  = len(buf) >= SAVE_BATCH_SIZE or (is_last and buf)

                    if flush_now and not is_last:
                        # Overlap: fetch + flush simultaneously
                        msgs, (sv, nd) = await asyncio.gather(
                            _bot_fetch(bot, chat_id, id_window, fw_count),
                            _flush(list(buf), target),
                        )
                        saved += sv; dup += nd
                        buf.clear()
                    else:
                        msgs = await _bot_fetch(bot, chat_id, id_window, fw_count)

                    calls    += 1
                    current   = end + 1
                    cur_id[0] = current

                    for msg in msgs:
                        checked += 1
                        doc = await _proc(msg)
                        if doc:
                            buf.append(doc)
                        else:
                            skipped += 1

                    # Last window flush
                    if is_last and buf:
                        sv, nd = await _flush(list(buf), target)
                        saved += sv; dup += nd
                        buf.clear()

                    # Checkpoint
                    cp_n += 1
                    if cp_n >= CHECKPOINT_EVERY or is_last:
                        await save_bot_state(chat_key, cur_id[0])
                        cp_n = 0

                    # Log every 100 calls
                    if calls % 100 == 0:
                        pct = (current - start_id) / total_span * 100
                        logger.info(
                            f"[BOT] call={calls} checked={checked:,} saved={saved} "
                            f"dup={dup} {pct:.1f}% {mps():.0f}msg/s"
                        )
                        gc.collect()

                    await push()
                    await asyncio.sleep(BOT_CALL_DELAY)

            # ── Done ─────────────────────────────────────────────
            await save_bot_state(chat_key, None)

            avg = int(mps())
            final = (
                f"✅ **Index Complete!**\n\n"
                f"🔧 **Mode**      : {'👤 User Session' if mode == 'user' else '🤖 Bot'}\n"
                f"📦 **Checked**   : `{checked:,}`\n"
                f"✅ **Saved**     : `{saved:,}` files\n"
                f"⏭ **Skipped**   : `{skipped:,}`\n"
                f"🔁 **Duplicate** : `{dup:,}`\n"
                f"📞 **API calls** : `{calls:,}`\n"
                f"⚡ **Avg speed** : `{avg:,}` msg/s\n"
            )
            if fw_count[0]:
                final += f"⏸ **FloodWait**  : `{fw_count[0]}` times\n"
            final += f"\n🎯 **Target** : `{target_label}`"
            await safe_edit(status_msg, final, parse_mode=enums.ParseMode.MARKDOWN)
            logger.info(
                f"[INDEX] ✅ done mode={mode} checked={checked} saved={saved} "
                f"dup={dup} skip={skipped} calls={calls} speed={avg}msg/s"
            )

        except asyncio.CancelledError:
            await save_bot_state(chat_key, cur_id[0])
            if buf:
                try:
                    sv, nd = await _flush(buf, target)
                    saved += sv; dup += nd
                except Exception:
                    pass
            await safe_edit(
                status_msg,
                f"⛔ **Index Cancelled**\n\n"
                f"🔧 **Mode**      : {'👤 User Session' if mode == 'user' else '🤖 Bot'}\n"
                f"📦 **Checked**   : `{checked:,}`\n"
                f"✅ **Saved**     : `{saved:,}` files\n"
                f"⏭ **Skipped**   : `{skipped:,}`\n"
                f"🔁 **Duplicate** : `{dup:,}`\n"
                f"⏹ Stopped at ID : `{cur_id[0]}`\n"
                f"🎯 **Target** : `{target_label}`",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            logger.info(f"[INDEX] cancelled at id={cur_id[0]} saved={saved}")

        finally:
            await terminate_index_client()   # ← ALWAYS disconnect user client
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
            set_caption_lang_cache(None)
            index_task = None


# ─────────────────────────────────────────────────────────────
# EFFECTIVE START
# ─────────────────────────────────────────────────────────────

async def compute_effective_start(chat_id: int, default_start: int) -> int:
    skip_raw = await get_global_setting(INDEX_SKIP_KEY, 0)
    try:
        skip_id = int(skip_raw) if skip_raw else 0
    except Exception:
        skip_id = 0
    lp = await get_bot_state(f"index_{chat_id}")
    return max(default_start, skip_id + 1, int(lp) if lp else 0)


# ─────────────────────────────────────────────────────────────
# SHARED UI HELPERS
# ─────────────────────────────────────────────────────────────

async def _mode_badge() -> str:
    has = await has_session_string()
    return "👤 **User mode** (session ✅)" if has else "🤖 **Bot mode** (no session)"


async def _est(chat_id, start_id, last_id) -> str:
    has      = await has_session_string()
    total    = last_id - start_id + 1
    if has:
        calls    = max(total // USER_BATCH, 1)
        est_sec  = int(calls * USER_CALL_DELAY)
    else:
        calls    = max(total // BOT_BATCH, 1)
        est_sec  = int(calls * BOT_CALL_DELAY)
    if est_sec < 60:
        return f"~{est_sec}s"
    elif est_sec < 3600:
        return f"~{est_sec // 60}m"
    else:
        return f"~{est_sec // 3600}h {est_sec % 3600 // 60}m"


# ─────────────────────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────────────────────

def register_index_handlers(cyber: Client):
    global index_task

    # ── Real-Time Index (channel listener) ───────────────────
    @cyber.on_message(media_filter & filters.channel)
    async def rti(_, msg):
        channels = await get_index_channels()
        if msg.chat.id not in channels:
            return
        try:
            file = msg.document or msg.video or msg.audio
            if not file:
                return
            ok, _ = await save_series_file_for_bulk_index(msg, target="both")
            fn    = getattr(file, "file_name", None) or "No_Name"
            if ok:
                from utils.cyber_index import parse_file_name
                m = parse_file_name(fn)
                logger.info(
                    f"[RTI] ✅ {m['normalized_name']!r} "
                    f"S{m['season']:02d}E{m['episode']:02d} "
                    f"{m['quality']} {m['language']} | {fn}"
                )
            else:
                logger.debug(f"[RTI] ⏭ {fn}")
        except Exception as e:
            logger.error(f"[RTI] {e}")

    # ── Forward a message to trigger index ───────────────────
    @cyber.on_message(
        filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter
    )
    async def forwarded(bot, message):
        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            return await message.reply("Forward an original message from a channel.")
        chat_id   = origin.chat.id
        last_id   = origin.message_id
        eff_start = await compute_effective_start(chat_id, 2)
        badge     = await _mode_badge()
        est       = await _est(chat_id, eff_start, last_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 DB1",   callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db1"),
             InlineKeyboardButton("📁 DB2",   callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db2")],
            [InlineKeyboardButton("📁📁 Both", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_prompt")],
        ])
        await bot.send_message(
            message.from_user.id,
            f"📦 **Index Target?**\n"
            f"Channel : `{chat_id}`\n"
            f"Range   : `{eff_start}` → `{last_id}`\n\n"
            f"{badge}  ·  Est. time: **{est}**\n\n"
            f"Choose DB target:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    # ── /indexlink command ────────────────────────────────────
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink"))
    async def indexlink(bot, message):
        args = message.text.split()[1:]
        chat_id, start_id, last_id = extract_links(args)
        eff_start = await compute_effective_start(chat_id, start_id)
        badge     = await _mode_badge()
        est       = await _est(chat_id, eff_start, last_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 DB1",   callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db1"),
             InlineKeyboardButton("📁 DB2",   callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db2")],
            [InlineKeyboardButton("📁📁 Both", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_prompt")],
        ])
        await bot.send_message(
            message.from_user.id,
            f"📦 **Index Target?**\n"
            f"Channel : `{chat_id}`\n"
            f"Range   : `{eff_start}` → `{last_id}`\n\n"
            f"{badge}  ·  Est. time: **{est}**\n\n"
            f"Choose DB target:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    # ── Choose DB ─────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex(r"^idx_db\|"))
    async def idx_db_chosen(bot, query):
        await query.answer()
        _, chat_id, s, l, target = query.data.split("|")
        chat_id, s, l = int(chat_id), int(s), int(l)
        badge = await _mode_badge()
        est   = await _est(chat_id, s, l)
        label = {"db1": "📁 DB1", "db2": "📁 DB2", "both": "📁📁 Both"}.get(target, target)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Proceed", callback_data=f"startidx|{chat_id}|{s}|{l}|{target}")],
            [InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
        ])
        try:
            await query.message.edit_text(
                f"**Confirm Index**\n\n"
                f"Channel : `{chat_id}`\n"
                f"Range   : `{s}` → `{l}` ({l-s+1:,} IDs)\n"
                f"Target  : **{label}**\n"
                f"{badge}\n"
                f"Est.    : **{est}**\n\n"
                f"Tap Proceed to start.",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        except Exception:
            pass

    # ── Start ─────────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex(r"^startidx\|"))
    async def start_index(bot, query):
        global index_task
        await query.answer()
        if index_task and not index_task.done():
            await query.answer("Index already running.", show_alert=True)
            return
        try:
            _, chat_id, s, l, target = query.data.split("|")
            chat_id, s, l = int(chat_id), int(s), int(l)
        except Exception as e:
            logger.error(f"[INDEX] startidx parse: {e}")
            return

        label    = {"db1": "DB1", "db2": "DB2", "both": "DB1+DB2"}.get(target, target)
        has_sess = await has_session_string()
        mode_str = "👤 User Session" if has_sess else "🤖 Bot"

        skip_raw = await get_global_setting(INDEX_SKIP_KEY, None)
        try:
            preserved = int(skip_raw) if skip_raw is not None else None
        except Exception:
            preserved = None

        await delete_all_index_states()
        if preserved is not None:
            await set_global_setting(INDEX_SKIP_KEY, preserved)
        await save_bot_state(f"index_{chat_id}", s)

        try:
            await query.message.edit_text(
                f"⏳ **Starting...** — {label} · {mode_str}",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        status_msg = await safe_send(
            bot, query.from_user.id,
            f"⚡ **Index Starting**\n"
            f"Channel : `{chat_id}`\n"
            f"Range   : `{s}` → `{l}`\n"
            f"Target  : **{label}**\n"
            f"Mode    : **{mode_str}**\n\n"
            f"_Progress updates every {PROGRESS_EVERY}s..._",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, s, l, target=target)
        )
        logger.info(f"[INDEX] task started chat={chat_id} target={target} mode={'user' if has_sess else 'bot'}")

    # ── Cancel ────────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex("^cancel_index$"))
    async def cancel(bot, query):
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("⛔ Cancelling...", show_alert=True)
        else:
            await query.answer("No active index.", show_alert=True)

    @cyber.on_callback_query(filters.regex("^cancel_prompt$"))
    async def cancel_prompt(bot, query):
        try: await query.message.delete()
        except Exception: pass
        await query.answer("Cancelled.")

    # ── Resume ────────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex(r"^resume_yes\|"))
    async def resume_yes(bot, query):
        global index_task
        if index_task and not index_task.done():
            await query.answer("Already running!", show_alert=True)
            return
        await query.answer("▶️ Resuming...")
        try:
            _, chat_id, s, l, target = query.data.split("|")
            chat_id, s, l = int(chat_id), int(s), int(l)
        except Exception as e:
            logger.error(f"resume_yes parse: {e}")
            return
        has_sess = await has_session_string()
        try:
            await query.message.edit_text(
                f"▶️ **Resuming...**\n"
                f"Channel `{chat_id}` · `{s}`→`{l}`\n"
                f"Mode: **{'👤 User Session' if has_sess else '🤖 Bot'}**",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=_cancel_kb(),
            )
        except Exception:
            pass
        index_task = asyncio.create_task(
            index_files_task(bot, query.message, chat_id, s, l, target=target)
        )

    @cyber.on_callback_query(filters.regex("^resume_no$"))
    async def resume_no(bot, query):
        await query.answer("Discarding.")
        try:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
            await query.message.edit_text("🗑 Discarded. Forward a message to start fresh.",
                                           parse_mode=enums.ParseMode.MARKDOWN)
        except Exception:
            pass

    # ── Skip helpers ──────────────────────────────────────────
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("setskip"))
    async def setskip(bot, message):
        parts = message.text.split()
        if len(parts) < 2:
            return await message.reply("Usage: /setskip <msg_id>")
        try:
            val = int(parts[1])
        except Exception:
            return await message.reply("Invalid ID.")
        await set_global_setting(INDEX_SKIP_KEY, val)
        await message.reply(f"✅ Skip set — index starts from `{val + 1}`",
                            parse_mode=enums.ParseMode.MARKDOWN)

    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("getskip"))
    async def getskip(bot, message):
        raw = await get_global_setting(INDEX_SKIP_KEY, None)
        await message.reply(f"Skip stored: `{raw!r}`", parse_mode=enums.ParseMode.MARKDOWN)

    return [
        rti, forwarded, indexlink, idx_db_chosen,
        start_index, cancel, cancel_prompt,
        resume_yes, resume_no, setskip, getskip,
    ]
