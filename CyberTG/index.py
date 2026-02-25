"""
index.py — BOT-SAFE EDITION
==============================

STRATEGY: get_messages(id_list) with small batches + mandatory pacing
-----------------------------------------------------------------------
Bots cannot use messages.GetHistory — only user accounts can.
Bots must use messages.GetMessages with explicit ID lists.

The FloodWait was caused by fetching 200 IDs per call — too heavy.

SOLUTION:
  - Fetch 50 IDs per call (not 200) — 4× lighter per request
  - 0.5s delay between batches (proven safe by reference implementation)
  - Adaptive: FloodWait → delay grows, clean calls → delay decays
  - No parallelism — one call at a time, steady and safe

THROUGHPUT ESTIMATE:
  50 IDs × (1 / 1.5s) = ~33 IDs/second = ~2000 IDs/minute
  For 500k IDs ≈ 4 hours  (vs hanging forever with FloodWaits)
  For 50k  IDs ≈ 25 minutes

All other features preserved: progress, cancel, resume, Yes/No restart.
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
    set_caption_lang_cache,
    batch_dedup_check,
)
from utils.cyber_filter import extract_links

# ─────────────────────────────────────────────────────────────
# TUNABLE CONSTANTS
# ─────────────────────────────────────────────────────────────

FETCH_BATCH_SIZE    = 100    # IDs per get_messages() call — halved to reduce per-call weight
MIN_CALL_DELAY      = 1.0    # seconds between batches — doubled from reference for safety
INTRA_BATCH_PAUSE   = 0.3    # tiny pause every 20 messages within a batch
SAVE_BATCH_SIZE     = 400    # accumulate docs before one DB write
PROGRESS_EDIT_EVERY = 30     # seconds between Telegram message edits

# Adaptive pacing
# On FloodWait: delay jumps to fw.value territory (already slept), then we add
# extra headroom so next call is slower. 4× means 1.0s → 4.0s after first FloodWait.
_RATE_INCREASE   = 4.0    # multiply delay on FloodWait (aggressive back-off)
_RATE_DECAY      = 0.97   # multiply delay each clean call (slow recovery)
_MAX_CALL_DELAY  = 120.0  # cap

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


_last_edit_text: dict[int, str] = {}


async def safe_edit(msg: Message, text: str, **kwargs):
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
    call_count: int,
    target_label: str,
    floodwait_count: int,
    call_delay: float,
    status_line: str = "⚡ Indexing...",
) -> str:
    total_range = max(last_id - start_id, 1)
    pct = min(((current - start_id) / total_range) * 100, 100.0)
    filled = int(pct / 5)
    bar    = "█" * filled + "░" * (20 - filled)

    lines = [
        f"{status_line}",
        f"",
        f"[{bar}] **{pct:.1f}%**",
        f"ID: `{current}` / `{last_id}`",
        f"",
        f"📦 **Checked**   : `{total_checked:,}` IDs  (`{call_count}` calls)",
        f"✅ **Saved**     : `{total_saved:,}` files",
        f"⏭ **Skipped**   : `{total_skipped:,}`",
        f"🔁 **Duplicate** : `{total_duplicate:,}`",
    ]

    if floodwait_count:
        lines.append(f"⏸ **FloodWait**  : `{floodwait_count}` times")

    lines += [
        f"",
        f"⏱ **Pace**    : `{call_delay:.1f}s` / call  ({FETCH_BATCH_SIZE} IDs/call)",
        f"🎯 **Target** : `{target_label}`",
    ]
    return "\n".join(lines)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ Cancel Indexing", callback_data="cancel_index")
    ]])


# ─────────────────────────────────────────────────────────────
# SINGLE BATCH FETCH  (with retry)
# ─────────────────────────────────────────────────────────────

async def _fetch_batch(
    bot: Client,
    chat_id: int,
    id_window: list[int],
    call_delay: list,        # mutable [float]
    floodwait_count: list,   # mutable [int]
) -> list:
    """
    Fetch one batch of message IDs using the proven reference pattern:
      - get_messages(200 IDs) — Telegram's native batch endpoint
      - FloodWait: sleep the required time + grow delay, then retry
      - Tiny intra-batch pause every 20 messages during processing
    """
    while True:
        try:
            msgs = await bot.get_messages(chat_id, id_window)
            if not isinstance(msgs, list):
                msgs = [msgs]
            return msgs
        except FloodWait as fw:
            floodwait_count[0] += 1
            call_delay[0] = min(call_delay[0] * _RATE_INCREASE, _MAX_CALL_DELAY)
            logger.warning(
                f"[INDEX] FloodWait {fw.value}s — sleeping, "
                f"new delay → {call_delay[0]:.1f}s"
            )
            await asyncio.sleep(fw.value + 1)
            # retry same batch after flood wait
        except ChannelPrivate:
            logger.error(f"[INDEX] Channel {chat_id} is private/inaccessible.")
            return []
        except Exception as e:
            logger.error(f"[INDEX] get_messages error: {e}")
            await asyncio.sleep(5)
            return []  # skip this batch on non-FloodWait error


# ─────────────────────────────────────────────────────────────
# CORE INDEX ENGINE
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

    # ── Resume from checkpoint ────────────────────────────────
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

    # Cache CAPTION_LANG once (eliminates per-doc MongoDB call)
    caption_lang_flag = await get_global_setting("CAPTION_LANG", True)
    set_caption_lang_cache(bool(caption_lang_flag))
    logger.info(f"[INDEX] CAPTION_LANG cached: {caption_lang_flag}")

    # Counters
    total_checked   = 0
    total_saved     = 0
    total_skipped   = 0
    total_duplicate = 0
    call_count      = 0
    floodwait_count = [0]
    call_delay      = [MIN_CALL_DELAY]

    last_edit_time  = [0.0]

    async def push_progress(force: bool = False, status_line: str = "⚡ Indexing..."):
        now = time.monotonic()
        if not force and (now - last_edit_time[0]) < PROGRESS_EDIT_EVERY:
            return
        text = _build_progress_text(
            current=current,
            start_id=start_id,
            last_id=last_id,
            total_checked=total_checked,
            total_saved=total_saved,
            total_skipped=total_skipped,
            total_duplicate=total_duplicate,
            call_count=call_count,
            target_label=target_label,
            floodwait_count=floodwait_count[0],
            call_delay=call_delay[0],
            status_line=status_line,
        )
        await safe_edit(
            status_msg, text,
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        last_edit_time[0] = time.monotonic()

    await push_progress(force=True)

    doc_buffer:     list[dict] = []
    last_checkpoint = current

    total_range = max(last_id - start_id, 1)
    total_calls = (total_range + FETCH_BATCH_SIZE - 1) // FETCH_BATCH_SIZE
    logger.info(
        f"[INDEX] Starting: chat={chat_id} from={current} → {last_id} "
        f"({total_range:,} IDs, ~{total_calls} calls, "
        f"{FETCH_BATCH_SIZE} IDs/call, {MIN_CALL_DELAY}s/call)"
    )

    try:
        while current <= last_id:

            # ── Build ID window ───────────────────────────────
            window_end = min(current + FETCH_BATCH_SIZE - 1, last_id)
            id_window  = list(range(current, window_end + 1))

            # ── Fetch ─────────────────────────────────────────
            prev_fw = floodwait_count[0]
            msgs    = await _fetch_batch(bot, chat_id, id_window, call_delay, floodwait_count)
            call_count += 1

            # Adapt delay based on whether FloodWait occurred
            if floodwait_count[0] > prev_fw:
                # FloodWait happened — delay already grown inside _fetch_batch
                pass
            else:
                # Clean call — slowly recover toward MIN_CALL_DELAY
                call_delay[0] = max(call_delay[0] * _RATE_DECAY, MIN_CALL_DELAY)

            # ── Parse media messages (intra-batch pause every 20) ───
            for i, msg in enumerate(msgs):
                if msg is None or msg.empty:
                    continue
                if not (msg.document or msg.video or msg.audio):
                    total_skipped += 1
                    continue
                doc = await parse_message_to_doc(msg)
                if doc is not None:
                    doc_buffer.append(doc)
                else:
                    total_skipped += 1

                # Tiny pause every 20 messages — matches reference impl
                if i > 0 and i % 20 == 0:
                    await asyncio.sleep(INTRA_BATCH_PAUSE)

            total_checked += len(id_window)
            current        = window_end + 1

            # ── Checkpoint ────────────────────────────────────
            if current != last_checkpoint:
                await save_bot_state(chat_key, current)
                last_checkpoint = current

            # ── Bulk save ─────────────────────────────────────
            is_last = current > last_id
            if len(doc_buffer) >= SAVE_BATCH_SIZE or (is_last and doc_buffer):
                to_save, n_dup = await batch_dedup_check(doc_buffer)
                total_duplicate += n_dup
                if to_save:
                    saved = await batch_save_file_docs(to_save, target=target)
                    total_saved += saved
                    for doc in to_save:
                        logger.info(
                            f"[BULK INDEX] ✅ {doc['normalized_name']!r} "
                            f"S{doc['season']:02d}E{doc['episode']:02d} "
                            f"{doc['quality']} {doc['language']}"
                        )
                doc_buffer.clear()

            # ── Console log every 100 calls ───────────────────
            if call_count % 100 == 0:
                pct = ((current - start_id) / total_range) * 100
                logger.info(
                    f"[INDEX] call={call_count} checked={total_checked:,} "
                    f"saved={total_saved} dup={total_duplicate} "
                    f"skip={total_skipped} {pct:.1f}% delay={call_delay[0]:.2f}s"
                )

            # ── Progress edit (30s throttle) ──────────────────
            await push_progress()

            # ── Mandatory inter-call delay ────────────────────
            await asyncio.sleep(call_delay[0])

            # ── Yield event loop every 200 calls ─────────────
            if call_count % 200 == 0:
                gc.collect()
                await asyncio.sleep(0)

        # ── Done ──────────────────────────────────────────────
        await save_bot_state(chat_key, None)

        final_text = (
            f"✅ **Index Complete!**\n\n"
            f"📦 **Checked**   : `{total_checked:,}` IDs\n"
            f"✅ **Saved**     : `{total_saved:,}` files\n"
            f"⏭ **Skipped**   : `{total_skipped:,}`\n"
            f"🔁 **Duplicate** : `{total_duplicate:,}`\n"
            f"📞 **API calls** : `{call_count:,}`\n"
        )
        if floodwait_count[0]:
            final_text += f"⏸ **FloodWait**  : `{floodwait_count[0]}` times\n"
        final_text += f"\n🎯 **Target** : `{target_label}`"

        await safe_edit(status_msg, final_text, parse_mode=enums.ParseMode.MARKDOWN)
        logger.info(
            f"[INDEX] ✅ Done — checked={total_checked} saved={total_saved} "
            f"dup={total_duplicate} skip={total_skipped} calls={call_count}"
        )

    except asyncio.CancelledError:
        await save_bot_state(chat_key, current)

        if doc_buffer:
            try:
                to_save, n_dup = await batch_dedup_check(doc_buffer)
                total_duplicate += n_dup
                if to_save:
                    saved = await batch_save_file_docs(to_save, target=target)
                    total_saved += saved
            except Exception:
                pass

        await safe_edit(
            status_msg,
            f"⛔ **Index Cancelled**\n\n"
            f"📦 **Checked**   : `{total_checked:,}` IDs\n"
            f"✅ **Saved**     : `{total_saved:,}` files\n"
            f"⏭ **Skipped**   : `{total_skipped:,}`\n"
            f"🔁 **Duplicate** : `{total_duplicate:,}`\n"
            f"\n⏹ Stopped at ID: `{current}`\n"
            f"🎯 **Target** : `{target_label}`",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        logger.info(f"[INDEX] Cancelled at id={current} saved={total_saved}")

    finally:
        await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        set_caption_lang_cache(None)
        index_task = None


# ─────────────────────────────────────────────────────────────
# EFFECTIVE START
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
# HANDLERS
# ─────────────────────────────────────────────────────────────

def register_index_handlers(cyber: Client):
    global index_task

    @cyber.on_message(media_filter & filters.channel)
    async def rti(_, msg):
        channels = await get_index_channels()
        if msg.chat.id not in channels:
            return
        try:
            file = msg.document or msg.video or msg.audio or msg.photo
            if not file:
                return
            ok, _ = await save_series_file_for_bulk_index(msg, target="both")
            if ok:
                from utils.cyber_index import parse_file_name
                fn   = getattr(file, "file_name", None) or "No_Name"
                meta = parse_file_name(fn)
                logger.info(
                    f"[RTI] ✅ {meta['normalized_name']!r} "
                    f"S{meta['season']:02d}E{meta['episode']:02d} "
                    f"{meta['quality']} {meta['language']} | {fn}"
                )
            else:
                fn = getattr(file, "file_name", None) or "No_Name"
                logger.debug(f"[RTI] ⏭ Skipped: {fn}")
        except Exception as e:
            logger.error(f"[RTI] error: {e}")

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
            [InlineKeyboardButton("📁📁 Both", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both")],
            [InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
        ])
        await bot.send_message(
            message.from_user.id,
            f"📦 **Index Target?**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{eff_start}** → **{last_id}**\n\n"
            f"⚙️ `{FETCH_BATCH_SIZE}` IDs/call · `{MIN_CALL_DELAY}s` min delay\n\n"
            f"Choose which MongoDB to write files into:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

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
            [InlineKeyboardButton("📁📁 Both", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both")],
            [InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
        ])
        await bot.send_message(
            message.from_user.id,
            f"📦 **Index Target?**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{eff_start}** → **{last_id}**\n\n"
            f"⚙️ `{FETCH_BATCH_SIZE}` IDs/call · `{MIN_CALL_DELAY}s` min delay\n\n"
            f"Choose which MongoDB to write files into:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    @cyber.on_callback_query(filters.regex(r"^idx_db\|"))
    async def idx_db_chosen(bot, query):
        await query.answer()
        parts = query.data.split("|")
        _, chat_id, start_id, last_id, target = parts
        chat_id, start_id, last_id = int(chat_id), int(start_id), int(last_id)
        total_ids = last_id - start_id + 1
        n_calls   = (total_ids + FETCH_BATCH_SIZE - 1) // FETCH_BATCH_SIZE
        label = {"db1": "📁 DB1 only", "db2": "📁 DB2 only", "both": "📁📁 DB1 + DB2"}.get(target, target)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Proceed", callback_data=f"startidx|{chat_id}|{start_id}|{last_id}|{target}")],
            [InlineKeyboardButton("❌ Cancel",  callback_data="cancel_prompt")],
        ])
        try:
            await query.message.edit_text(
                f"**Confirm Index**\n\n"
                f"Channel  : `{chat_id}`\n"
                f"Range    : **{start_id}** → **{last_id}** ({total_ids:,} IDs)\n"
                f"Target   : **{label}**\n"
                f"API Calls: ~**{n_calls:,}** × {FETCH_BATCH_SIZE} IDs\n"
                f"Min delay: **{MIN_CALL_DELAY}s** / call\n\n"
                f"Tap Proceed to start.",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        except Exception as e:
            logger.debug(f"[INDEX] idx_db_chosen edit skipped: {e}")

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
            logger.error(f"[INDEX] parse error: {e}")
            return

        label = {"db1": "DB1 only", "db2": "DB2 only", "both": "DB1 + DB2"}.get(target, target)

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
                f"⏳ **Starting index...**\nTarget: **{label}**",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        status_msg = await safe_send(
            bot, query.from_user.id,
            f"⚡ **Index Starting**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{start_id}** → **{last_id}**\n"
            f"Target: **{label}**\n"
            f"⚙️ `{FETCH_BATCH_SIZE}` IDs/call · adaptive delay\n\n"
            f"_Progress updates every 30 seconds..._",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )

        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, start_id, last_id, target=target)
        )
        logger.info(f"[INDEX] Task created — chat={chat_id} target={target}")

    @cyber.on_callback_query(filters.regex("^cancel_index$"))
    async def cancel(bot, query):
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("⛔ Cancelling...", show_alert=True)
        else:
            await query.answer("No active index to cancel.", show_alert=True)
            try:
                await query.message.edit_text("ℹ️ No active index was running.", parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass

    @cyber.on_callback_query(filters.regex("^cancel_prompt$"))
    async def cancel_prompt(bot, query):
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer("Cancelled.")

    @cyber.on_callback_query(filters.regex(r"^resume_yes\|"))
    async def resume_yes(bot, query):
        global index_task
        if index_task and not index_task.done():
            await query.answer("⚠️ Index already running!", show_alert=True)
            return
        await query.answer("▶️ Resuming...")
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
            index_files_task(bot, query.message, chat_id, start_msg_id, last_msg_id, target=target)
        )
        logger.info(f"[INDEX] Resume task started — chat={chat_id} target={target}")

    @cyber.on_callback_query(filters.regex("^resume_no$"))
    async def resume_no(bot, query):
        await query.answer("🗑 Discarding.")
        try:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        except Exception:
            pass
        try:
            await query.message.edit_text(
                "🗑 **Interrupted index discarded.**\nStart fresh by forwarding a message.",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("setskip"))
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

    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("getskip"))
    async def getskip(bot, message):
        raw = await get_global_setting(INDEX_SKIP_ID_KEY, None)
        await message.reply(f"Skip stored: **{raw!r}**")

    return [
        rti, forwarded, indexlink, idx_db_chosen,
        start_index, cancel, cancel_prompt,
        resume_yes, resume_no,
        setskip, getskip,
    ]
