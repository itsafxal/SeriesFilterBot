"""
index.py — PROGRESS EDITION
=====================================================
IMPROVEMENTS OVER PREVIOUS VERSION:
  1. Rich progress message: Batched, Saved, Skipped, Duplicate, FloodWait status
  2. Progress edits throttled to every 30 seconds (avoids FloodWait + edit errors)
  3. Cancel button always visible on status message during indexing
  4. On bot restart with interrupted index → sends Yes/No resume prompt to admin
     (instead of auto-resuming silently)
  5. "Yes" resumes the index, "No" clears the state cleanly

BATCH INDEXING DESIGN (unchanged):
  1. Collects a window of up to FETCH_BATCH_SIZE (200) message IDs.
  2. Calls bot.get_messages(chat_id, list_of_ids) — one Telegram API call.
  3. Parses every media message in the batch (pure CPU, zero I/O).
  4. Bulk-saves all parsed docs to SQLite + MongoDB (one bulk_write per DB).
  5. No fixed sleep — runs full speed. FloodWait handled in fetch_batch.
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
    get_bot_state,
    save_bot_state,
    get_global_setting,
    set_global_setting,
    delete_all_index_states,
    get_index_channels,
)
from utils.cyber_index import (
    parse_message_to_doc,
    batch_save_file_docs,
    save_series_file_for_bulk_index,
)
from utils.cyber_filter import extract_links

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

FETCH_BATCH_SIZE      = 200    # IDs per get_messages() call (Telegram max)
LOG_EVERY             = 5      # log to console every N batches
YIELD_EVERY           = 10     # asyncio.sleep(0) every N batches
PROGRESS_EDIT_EVERY   = 30     # seconds between Telegram message edits

INDEX_SKIP_ID_KEY = "INDEX_SKIP_ID"
INDEX_STATUS_KEY  = "CURRENT_INDEX_TASK"

lock         = asyncio.Lock()
media_filter = filters.document | filters.video | filters.audio
index_task: Optional[asyncio.Task] = None


# ─────────────────────────────────────────────────────────────
# SAFE WRAPPERS
# ─────────────────────────────────────────────────────────────

async def safe_floodwait_sleep(exc: FloodWait):
    wait = max(int(getattr(exc, "value", 10)), 5)
    logger.warning(f"[INDEX] FloodWait {wait}s — sleeping...")
    await asyncio.sleep(wait + 2)


async def safe_send(bot: Client, uid: int, text: str, **kwargs):
    while True:
        try:
            return await bot.send_message(uid, text, **kwargs)
        except FloodWait as f:
            await safe_floodwait_sleep(f)
        except Exception as e:
            logger.error(f"[INDEX] safe_send failed: {e}")
            return None


_last_edit_text: dict[int, str] = {}   # msg_id -> last text sent


async def safe_edit(msg: Message, text: str, **kwargs):
    """
    Edit only if the text changed since last edit.
    Prevents MessageNotModified errors entirely.
    """
    if msg is None:
        return
    mid = msg.id
    if _last_edit_text.get(mid) == text:
        return
    try:
        await msg.edit_text(text, **kwargs)
        _last_edit_text[mid] = text
    except FloodWait as f:
        await safe_floodwait_sleep(f)
        try:
            await msg.edit_text(text, **kwargs)
            _last_edit_text[mid] = text
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[INDEX] safe_edit ignored: {e}")


# ─────────────────────────────────────────────────────────────
# PROGRESS BUILDER
# ─────────────────────────────────────────────────────────────

def _build_progress_text(
    *,
    current: int,
    start_id: int,
    last_id: int,
    total_checked: int,
    total_saved: int,
    total_skipped: int,
    total_duplicate: int,
    batch_count: int,
    target_label: str,
    floodwait_count: int,
    status_line: str = "⚡ Indexing...",
) -> str:
    total_range = max(last_id - start_id, 1)
    pct = min(((current - start_id) / total_range) * 100, 100.0)

    # Progress bar (20 chars wide)
    filled = int(pct / 5)
    bar    = "█" * filled + "░" * (20 - filled)

    lines = [
        f"{status_line}",
        f"",
        f"[{bar}] **{pct:.1f}%**",
        f"ID: `{current}` / `{last_id}`",
        f"",
        f"📦 **Batched**   : `{total_checked:,}` msgs  (`{batch_count}` batches)",
        f"✅ **Saved**     : `{total_saved:,}` files",
        f"⏭ **Skipped**   : `{total_skipped:,}`",
        f"🔁 **Duplicate** : `{total_duplicate:,}`",
    ]

    if floodwait_count:
        lines.append(f"⏸ **FloodWait**  : `{floodwait_count}` times")

    lines += [
        f"",
        f"🎯 **Target** : `{target_label}`",
    ]

    return "\n".join(lines)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ Cancel Indexing", callback_data="cancel_index")
    ]])


# ─────────────────────────────────────────────────────────────
# BATCH FETCH — get up to 200 messages in a single API call
# ─────────────────────────────────────────────────────────────

async def fetch_batch(bot: Client, chat_id: int, ids: list[int]) -> list:
    while True:
        try:
            msgs = await bot.get_messages(chat_id, ids)
            if not isinstance(msgs, list):
                msgs = [msgs]
            return msgs
        except FloodWait as fw:
            await safe_floodwait_sleep(fw)
        except ChannelPrivate:
            logger.error(f"[INDEX] Channel {chat_id} is private/inaccessible.")
            return []
        except Exception as e:
            logger.error(f"[INDEX] fetch_batch error: {e}")
            await asyncio.sleep(3)
            return []


# ─────────────────────────────────────────────────────────────
# CORE INDEX ENGINE — BATCH VERSION WITH RICH PROGRESS
# ─────────────────────────────────────────────────────────────

async def index_files_task(
    bot: Client,
    status_msg: Message,
    chat_id: int,
    start_id: int,
    last_id: int,
    target: str = "both",
):
    global index_task

    chat_key     = f"index_{chat_id}"
    target_label = {"db1": "DB1 only", "db2": "DB2 only", "both": "DB1 + DB2"}.get(target, target)

    # ── Resume: start from last checkpoint ───────────────────
    last_processed = await get_bot_state(chat_key)
    skip_raw = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
    try:
        skip_id = int(skip_raw)
    except Exception:
        skip_id = 0

    candidates = [start_id, skip_id + 1]
    if last_processed:
        try:
            candidates.append(int(last_processed))
        except Exception:
            pass

    current = max(candidates)

    await set_global_setting(
        INDEX_STATUS_KEY,
        f"RUNNING|{chat_id}|{start_id}|{last_id}|{target}",
    )

    total_checked   = 0
    total_saved     = 0
    total_skipped   = 0    # media msgs with no filename / no series name
    total_duplicate = 0    # already-in-db duplicates
    batch_count     = 0
    floodwait_count = 0

    # Time-throttled progress edit (every 30s)
    last_edit_time = 0.0

    async def push_progress(force: bool = False, status_line: str = "⚡ Indexing..."):
        nonlocal last_edit_time
        now = time.monotonic()
        if not force and (now - last_edit_time) < PROGRESS_EDIT_EVERY:
            return
        text = _build_progress_text(
            current=current,
            start_id=start_id,
            last_id=last_id,
            total_checked=total_checked,
            total_saved=total_saved,
            total_skipped=total_skipped,
            total_duplicate=total_duplicate,
            batch_count=batch_count,
            target_label=target_label,
            floodwait_count=floodwait_count,
            status_line=status_line,
        )
        await safe_edit(
            status_msg, text,
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        last_edit_time = time.monotonic()

    # Initial edit
    await push_progress(force=True)

    try:
        while current <= last_id:

            # ── Build next window of IDs ──────────────────────
            window_end = min(current + FETCH_BATCH_SIZE - 1, last_id)
            id_window  = list(range(current, window_end + 1))

            # ── Fetch batch from Telegram (1 API call) ────────
            try:
                messages = await fetch_batch(bot, chat_id, id_window)
            except FloodWait as fw:
                floodwait_count += 1
                await safe_floodwait_sleep(fw)
                continue

            # ── Parse all media messages in batch ─────────────
            docs          = []
            batch_skipped = 0
            for msg in messages:
                if msg is None or msg.empty:
                    continue
                if not (msg.document or msg.video or msg.audio):
                    continue
                doc = await parse_message_to_doc(msg)
                if doc is not None:
                    docs.append(doc)
                else:
                    batch_skipped += 1

            total_checked  += len(id_window)
            total_skipped  += batch_skipped

            # ── Bulk save to Mongo + SQLite ───────────────────
            if docs:
                # batch_save_file_docs returns count of actually-saved docs.
                # docs that were rejected as duplicates = len(docs) - saved
                saved_count = await batch_save_file_docs(docs, target=target)
                dup_count   = len(docs) - saved_count
                total_saved     += saved_count
                total_duplicate += dup_count

                for doc in docs:
                    logger.info(
                        f"[BULK INDEX] ✅ Saved │ "
                        f"Series: {doc['normalized_name']!r} │ "
                        f"S{doc['season']:02d}E{doc['episode']:02d} │ "
                        f"{doc['quality']} │ {doc['language']} │ "
                        f"MsgID: {doc['message_id']} │ File: {doc['file_name']}"
                    )

            batch_count += 1
            current      = window_end + 1

            # ── Checkpoint after every batch ─────────────────
            await save_bot_state(chat_key, current)

            # ── Console log every N batches ───────────────────
            if batch_count % LOG_EVERY == 0:
                pct = ((current - start_id) / max(last_id - start_id, 1)) * 100
                logger.info(
                    f"[INDEX] batch={batch_count} checked={total_checked} "
                    f"saved={total_saved} dup={total_duplicate} "
                    f"skipped={total_skipped} {pct:.1f}% id={current}/{last_id}"
                )

            # ── Time-throttled Telegram progress edit (30s) ──
            await push_progress()

            # ── Yield event loop every N batches ─────────────
            if batch_count % YIELD_EVERY == 0:
                await asyncio.sleep(0)
                gc.collect()

        # ── Done — final edit ─────────────────────────────────
        await save_bot_state(chat_key, None)

        final_text = (
            f"✅ **Index Complete!**\n\n"
            f"📦 **Batched**   : `{total_checked:,}` msgs  (`{batch_count}` batches)\n"
            f"✅ **Saved**     : `{total_saved:,}` files\n"
            f"⏭ **Skipped**   : `{total_skipped:,}`\n"
            f"🔁 **Duplicate** : `{total_duplicate:,}`\n"
        )
        if floodwait_count:
            final_text += f"⏸ **FloodWait**  : `{floodwait_count}` times\n"
        final_text += f"\n🎯 **Target** : `{target_label}`"

        await safe_edit(
            status_msg, final_text,
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        logger.info(
            f"[INDEX] ✅ Done — checked={total_checked} saved={total_saved} "
            f"dup={total_duplicate} skipped={total_skipped} "
            f"batches={batch_count} target={target}"
        )

    except asyncio.CancelledError:
        await save_bot_state(chat_key, current)
        cancel_text = (
            f"⛔ **Index Cancelled**\n\n"
            f"📦 **Batched**   : `{total_checked:,}` msgs\n"
            f"✅ **Saved**     : `{total_saved:,}` files\n"
            f"⏭ **Skipped**   : `{total_skipped:,}`\n"
            f"🔁 **Duplicate** : `{total_duplicate:,}`\n"
            f"\n⏹ Stopped at ID: `{current}`\n"
            f"🎯 **Target** : `{target_label}`"
        )
        await safe_edit(
            status_msg, cancel_text,
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        logger.info(
            f"[INDEX] Cancelled at id={current} "
            f"saved={total_saved} dup={total_duplicate} skipped={total_skipped}"
        )

    finally:
        await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        index_task = None


# ─────────────────────────────────────────────────────────────
# EFFECTIVE START CALCULATOR
# ─────────────────────────────────────────────────────────────

async def compute_effective_start(chat_id: int, default_start: int):
    skip_raw = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
    try:
        skip_id = int(skip_raw)
    except Exception:
        skip_id = 0

    last_processed = await get_bot_state(f"index_{chat_id}")
    lp = int(last_processed) if last_processed else 0
    return max(default_start, skip_id + 1, lp)


# ─────────────────────────────────────────────────────────────
# REGISTER HANDLERS
# ─────────────────────────────────────────────────────────────

def register_index_handlers(cyber: Client):
    global index_task

    # ── RTI: real-time auto-index on new channel messages ────
    @cyber.on_message(media_filter & filters.channel)
    async def rti(_, msg):
        channels = await get_index_channels()
        if msg.chat.id not in channels:
            return
        try:
            file = msg.document or msg.video or msg.audio or msg.photo
            if not file:
                return

            ok, cnt = await save_series_file_for_bulk_index(msg, target="both")

            if ok:
                from utils.cyber_index import parse_file_name
                file_name = getattr(file, "file_name", None) or "No_Name"
                meta      = parse_file_name(file_name)
                logger.info(
                    f"[RTI] ✅ Saved │ "
                    f"Series: {meta['normalized_name']!r} │ "
                    f"S{meta['season']:02d}E{meta['episode']:02d} │ "
                    f"{meta['quality']} │ "
                    f"{meta['language']} │ "
                    f"File: {file_name}"
                )
            else:
                file_name = getattr(file, "file_name", None) or "No_Name"
                logger.debug(f"[RTI] ⏭ Skipped (duplicate or no name): {file_name}")

        except Exception as e:
            logger.error(f"[RTI] error: {e}")

    # ── FORWARDED message → ask DB target ────────────────────
    @cyber.on_message(
        filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter
    )
    async def forwarded(bot, message):
        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            return await message.reply("Forward an ORIGINAL message from a channel.")

        chat_id   = origin.chat.id
        last_id   = origin.message_id
        eff_start = await compute_effective_start(chat_id, 2)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 DB1",    callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db1"),
                InlineKeyboardButton("📁 DB2",    callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db2"),
            ],
            [
                InlineKeyboardButton("📁📁 Both", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both"),
            ],
            [
                InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt"),
            ],
        ])

        await bot.send_message(
            message.from_user.id,
            f"📦 **Index Target?**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{eff_start}** → **{last_id}**\n\n"
            f"Batch: **{FETCH_BATCH_SIZE}** msgs/call  |  Full speed (no sleep)\n\n"
            f"Choose which MongoDB to write files into:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    # ── /indexlink → same flow ────────────────────────────────
    @cyber.on_message(
        filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink")
    )
    async def indexlink(bot, message):
        args = message.text.split()[1:]
        chat_id, start_id, last_id = extract_links(args)
        eff_start = await compute_effective_start(chat_id, start_id)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 DB1",    callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db1"),
                InlineKeyboardButton("📁 DB2",    callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db2"),
            ],
            [
                InlineKeyboardButton("📁📁 Both", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both"),
            ],
            [
                InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt"),
            ],
        ])

        await bot.send_message(
            message.from_user.id,
            f"📦 **Index Target?**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{eff_start}** → **{last_id}**\n\n"
            f"Batch: **{FETCH_BATCH_SIZE}** msgs/call  |  Full speed (no sleep)\n\n"
            f"Choose which MongoDB to write files into:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    # ── DB target chosen → show final confirmation ────────────
    @cyber.on_callback_query(filters.regex(r"^idx_db\|"))
    async def idx_db_chosen(bot, query):
        await query.answer()
        parts = query.data.split("|")
        _, chat_id, start_id, last_id, target = parts
        chat_id, start_id, last_id = int(chat_id), int(start_id), int(last_id)

        total_ids  = last_id - start_id + 1
        n_batches  = (total_ids + FETCH_BATCH_SIZE - 1) // FETCH_BATCH_SIZE

        label = {
            "db1":  "📁 DB1 only",
            "db2":  "📁 DB2 only",
            "both": "📁📁 DB1 + DB2",
        }.get(target, target)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Proceed", callback_data=f"startidx|{chat_id}|{start_id}|{last_id}|{target}"),
            ],
            [
                InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt"),
            ],
        ])

        try:
            await query.message.edit_text(
                f"**Confirm Batch Index**\n\n"
                f"Channel : `{chat_id}`\n"
                f"Range   : **{start_id}** → **{last_id}** ({total_ids:,} IDs)\n"
                f"Target  : **{label}**\n"
                f"Batches : ~**{n_batches}** × {FETCH_BATCH_SIZE} msgs  |  Full speed\n\n"
                f"Tap Proceed to start.",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        except Exception as e:
            logger.debug(f"[INDEX] idx_db_chosen edit skipped: {e}")

    # ── START INDEX ───────────────────────────────────────────
    @cyber.on_callback_query(filters.regex(r"^startidx\|"))
    async def start_index(bot, query):
        global index_task

        await query.answer()

        if index_task and not index_task.done():
            await query.answer("Index already running.", show_alert=True)
            return

        try:
            parts    = query.data.split("|")
            chat_id  = int(parts[1])
            start_id = int(parts[2])
            last_id  = int(parts[3])
            target   = parts[4]
        except Exception as e:
            logger.error(f"[INDEX] start_index parse error: {e} | data={query.data!r}")
            try:
                await query.message.edit_text(
                    f"❌ Failed to parse index data.\n`{query.data}`",
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            return

        label = {"db1": "DB1 only", "db2": "DB2 only", "both": "DB1 + DB2"}.get(target, target)
        logger.info(f"[INDEX] Starting batch index — chat={chat_id} {start_id}→{last_id} target={target}")

        # Clear old states, preserve skip
        skip_raw = await get_global_setting(INDEX_SKIP_ID_KEY, None)
        try:
            preserved_skip = int(skip_raw) if skip_raw is not None else None
        except Exception:
            preserved_skip = None

        await delete_all_index_states()

        if preserved_skip is not None:
            await set_global_setting(INDEX_SKIP_ID_KEY, preserved_skip)

        await save_bot_state(f"index_{chat_id}", start_id)

        try:
            await query.message.edit_text(
                f"⏳ **Starting batch index...**\nTarget: **{label}**",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        status_msg = await safe_send(
            bot,
            query.from_user.id,
            f"⚡ **Batch Index Starting**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{start_id}** → **{last_id}**\n"
            f"Target: **{label}**\n\n"
            f"_Progress updates every 30 seconds..._",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )

        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, start_id, last_id, target=target)
        )
        logger.info(f"[INDEX] Batch task created — chat={chat_id} target={target}")

    # ── CANCEL INDEX ──────────────────────────────────────────
    @cyber.on_callback_query(filters.regex("^cancel_index$"))
    async def cancel(bot, query):
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("⛔ Cancelling index...", show_alert=True)
        else:
            await query.answer("No active index to cancel.", show_alert=True)
            try:
                await query.message.edit_text(
                    "ℹ️ No active index was running.",
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            except Exception:
                pass

    # ── CANCEL PROMPT ─────────────────────────────────────────
    @cyber.on_callback_query(filters.regex("^cancel_prompt$"))
    async def cancel_prompt(bot, query):
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer("Cancelled.")

    # ── RESUME YES ────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex(r"^resume_yes\|"))
    async def resume_yes(bot, query):
        global index_task
        await query.answer("▶️ Resuming index...")

        try:
            parts        = query.data.split("|")
            chat_id      = int(parts[1])
            start_msg_id = int(parts[2])
            last_msg_id  = int(parts[3])
            target       = parts[4]
        except Exception as e:
            logger.error(f"[INDEX] resume_yes parse error: {e}")
            try:
                await query.message.edit_text("❌ Failed to parse resume data.")
            except Exception:
                pass
            return

        label = {"db1": "DB1 only", "db2": "DB2 only", "both": "DB1 + DB2"}.get(target, target)

        try:
            await query.message.edit_text(
                f"▶️ **Resuming Index...**\n"
                f"Channel: `{chat_id}`\n"
                f"Range: **{start_msg_id}** → **{last_msg_id}**\n"
                f"Target: **{label}**\n\n"
                f"_Progress updates every 30 seconds..._",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=_cancel_kb(),
            )
        except Exception:
            pass

        index_task = asyncio.create_task(
            index_files_task(
                bot, query.message,
                chat_id, start_msg_id, last_msg_id,
                target=target,
            )
        )
        logger.info(f"[INDEX] Resume task started via button — chat={chat_id} target={target}")

    # ── RESUME NO ─────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex("^resume_no$"))
    async def resume_no(bot, query):
        await query.answer("🗑 Discarding interrupted index.")
        try:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        except Exception:
            pass
        try:
            await query.message.edit_text(
                "🗑 **Interrupted index discarded.**\n"
                "Start a fresh index with /index or by forwarding a message.",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    # ── /setskip ──────────────────────────────────────────────
    @cyber.on_message(
        filters.private & filters.user(ADMIN_USERS) & filters.command("setskip")
    )
    async def setskip(bot, message):
        parts = message.text.split()
        if len(parts) < 2:
            return await message.reply("Usage: /setskip <msg_id>")
        try:
            val = int(parts[1])
        except Exception:
            return await message.reply("Invalid ID.")
        await set_global_setting(INDEX_SKIP_ID_KEY, val)
        await message.reply(f"Skip set. Index will start from **{val + 1}**")

    # ── /getskip ──────────────────────────────────────────────
    @cyber.on_message(
        filters.private & filters.user(ADMIN_USERS) & filters.command("getskip")
    )
    async def getskip(bot, message):
        raw = await get_global_setting(INDEX_SKIP_ID_KEY, None)
        await message.reply(f"Skip stored: **{raw!r}**")

    return [
        rti, forwarded, indexlink, idx_db_chosen,
        start_index, cancel, cancel_prompt,
        resume_yes, resume_no,
        setskip, getskip,
    ]
