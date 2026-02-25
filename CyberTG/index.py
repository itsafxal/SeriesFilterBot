"""
index.py — ULTRA-FAST PARALLEL EDITION
========================================

SPEED ARCHITECTURE:
  ┌─────────────────────────────────────────────────────────────────┐
  │  PRODUCER/CONSUMER PIPELINE with asyncio.Queue                  │
  │                                                                  │
  │  Fetcher coroutines (CONCURRENT_FETCHES = 5):                   │
  │    Each fetches 200 msgs from Telegram in one get_messages()    │
  │    call, parses all docs (pure CPU), puts result into           │
  │    save_queue.                                                   │
  │                                                                  │
  │  Saver coroutine (1, serialized):                               │
  │    Drains save_queue, accumulates a SAVE_BATCH (up to 1000      │
  │    docs), deduplicates, bulk-saves to Mongo + SQLite in one     │
  │    round-trip. Serialized to avoid SQLite write conflicts.       │
  │                                                                  │
  │  Result: 5× parallel Telegram fetches, zero idle time on DB.   │
  └─────────────────────────────────────────────────────────────────┘

KEY OPTIMISATIONS vs previous version:
  1. CONCURRENT FETCHES — 5 simultaneous get_messages() calls
     (Telegram allows burst; FloodWait auto-handled per fetcher)
  2. CAPTION_LANG cached ONCE at start — eliminates MongoDB round-trip
     per document (was the #1 bottleneck — 200 DB calls per batch!)
  3. BATCH DEDUP via single SQLite IN() query — replaces N individual
     episode_exists() calls with 1 query for the whole batch
  4. SEMAPHORE-guarded fetches — respects Telegram rate limits safely
  5. SAVE_BATCH accumulation — multiple fetch results merged before
     one DB write, maximising bulk_write efficiency
  6. Progress edits throttled to 30s (unchanged, safe)
  7. Cancel button always visible (unchanged)
  8. Yes/No resume prompt on restart (unchanged)
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
    set_caption_lang_cache,           # NEW — see cyber_index.py
    batch_dedup_check,                # NEW — see cyber_index.py
)
from utils.cyber_filter import extract_links

# ─────────────────────────────────────────────────────────────
# TUNABLE CONSTANTS
# ─────────────────────────────────────────────────────────────

FETCH_BATCH_SIZE    = 200    # IDs per get_messages() call (Telegram max)
CONCURRENT_FETCHES  = 3      # max parallel fetchers (safe default; raise if no FloodWaits)
STAGGER_DELAY       = 0.5    # seconds between launching each fetcher in a group
MIN_INTER_BATCH     = 0.3    # minimum gap between consecutive fetch completions
SAVE_BATCH_SIZE     = 600    # accumulate this many docs before one DB write
PROGRESS_EDIT_EVERY = 30     # seconds between Telegram message edits

# Adaptive back-off: after FloodWait → multiply delay; each clean batch → decay toward MIN
_RATE_INCREASE   = 2.5   # multiply inter-batch delay by this on FloodWait
_RATE_DECAY      = 0.92  # multiply inter-batch delay by this each clean batch
_MAX_INTER_BATCH = 45.0  # never wait more than this between batches

INDEX_SKIP_ID_KEY = "INDEX_SKIP_ID"
INDEX_STATUS_KEY  = "CURRENT_INDEX_TASK"

lock         = asyncio.Lock()
media_filter = filters.document | filters.video | filters.audio
index_task: Optional[asyncio.Task] = None

_SENTINEL = object()


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
    batch_count: int,
    target_label: str,
    floodwait_count: int,
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
        f"🚀 **Mode**   : `{CONCURRENT_FETCHES}×` parallel · `{STAGGER_DELAY}s` stagger",
    ]

    return "\n".join(lines)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ Cancel Indexing", callback_data="cancel_index")
    ]])


# ─────────────────────────────────────────────────────────────
# SINGLE BATCH FETCHER — staggered, runs concurrently
# ─────────────────────────────────────────────────────────────

async def _fetch_one_window(
    bot: Client,
    chat_id: int,
    id_window: list[int],
    semaphore: asyncio.Semaphore,
    floodwait_counter: list,    # mutable [int] shared ref
    stagger_offset: float = 0.0,  # seconds to sleep before acquiring semaphore
) -> tuple[list, int, int]:
    """
    Fetch one window, parse all media docs.
    Returns: (docs, n_checked, n_skipped)

    stagger_offset: each fetcher in a group is launched with a small delay
    (0s, 0.5s, 1.0s, ...) so they don't all hit Telegram at the same instant.
    """
    # Stagger: wait before competing for the semaphore
    if stagger_offset > 0:
        await asyncio.sleep(stagger_offset)

    async with semaphore:
        # ── Fetch from Telegram ───────────────────────────────
        while True:
            try:
                msgs = await bot.get_messages(chat_id, id_window)
                if not isinstance(msgs, list):
                    msgs = [msgs]
                break
            except FloodWait as fw:
                floodwait_counter[0] += 1
                await safe_floodwait_sleep(fw)
            except ChannelPrivate:
                logger.error(f"[INDEX] Channel {chat_id} is private/inaccessible.")
                return [], len(id_window), 0
            except Exception as e:
                logger.error(f"[INDEX] fetch error: {e}")
                await asyncio.sleep(3)
                return [], len(id_window), 0

    # ── Parse (pure CPU — outside semaphore) ─────────────────
    docs    = []
    skipped = 0
    for msg in msgs:
        if msg is None or msg.empty:
            continue
        if not (msg.document or msg.video or msg.audio):
            continue
        doc = await parse_message_to_doc(msg)
        if doc is not None:
            docs.append(doc)
        else:
            skipped += 1

    return docs, len(id_window), skipped


# ─────────────────────────────────────────────────────────────
# CORE INDEX ENGINE — PARALLEL PIPELINE
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

    # ── Cache CAPTION_LANG once — eliminates per-doc MongoDB hit ──
    caption_lang_flag = await get_global_setting("CAPTION_LANG", True)
    set_caption_lang_cache(bool(caption_lang_flag))
    logger.info(f"[INDEX] CAPTION_LANG cached: {caption_lang_flag}")

    # ── Shared counters (mutated by saver) ────────────────────
    total_checked   = 0
    total_saved     = 0
    total_skipped   = 0
    total_duplicate = 0
    batch_count     = 0
    floodwait_count = [0]  # mutable ref passed to fetchers

    # ── Progress edit (30s throttle) ─────────────────────────
    last_edit_time = [0.0]

    async def push_progress(force: bool = False, status_line: str = "⚡ Parallel Indexing..."):
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
            batch_count=batch_count,
            target_label=target_label,
            floodwait_count=floodwait_count[0],
            status_line=status_line,
        )
        await safe_edit(
            status_msg, text,
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        last_edit_time[0] = time.monotonic()

    await push_progress(force=True)

    # ── Semaphore limits concurrent Telegram API calls ────────
    semaphore = asyncio.Semaphore(CONCURRENT_FETCHES)

    # ── Adaptive inter-batch delay — starts at MIN, grows on FloodWait ──
    inter_batch_delay = [MIN_INTER_BATCH]   # mutable so inner scope can write it

    try:
        # Build list of ALL windows upfront
        windows = []
        pos = current
        while pos <= last_id:
            end = min(pos + FETCH_BATCH_SIZE - 1, last_id)
            windows.append(list(range(pos, end + 1)))
            pos = end + 1

        total_windows = len(windows)
        logger.info(
            f"[INDEX] Starting staggered parallel index: "
            f"{total_windows} windows × {FETCH_BATCH_SIZE} IDs, "
            f"up to {CONCURRENT_FETCHES} concurrent, stagger={STAGGER_DELAY}s"
        )

        doc_buffer: list[dict] = []
        last_checkpoint = current
        prev_fw_count   = 0   # track FloodWait growth for rate adaptation

        for group_start in range(0, total_windows, CONCURRENT_FETCHES):
            group = windows[group_start : group_start + CONCURRENT_FETCHES]

            # ── Staggered parallel fetches ────────────────────
            # Each coroutine gets a stagger_offset = i * STAGGER_DELAY
            # so fetcher 0 fires immediately, fetcher 1 fires 0.5s later, etc.
            # This spreads the Telegram API load over CONCURRENT_FETCHES * 0.5s
            # instead of hammering it all at once.
            results = await asyncio.gather(
                *[
                    _fetch_one_window(
                        bot, chat_id, win, semaphore, floodwait_count,
                        stagger_offset=i * STAGGER_DELAY,
                    )
                    for i, win in enumerate(group)
                ],
                return_exceptions=False,
            )

            # ── Adaptive rate adjustment ──────────────────────
            new_fw = floodwait_count[0] - prev_fw_count
            if new_fw > 0:
                # FloodWait(s) occurred in this group → slow down
                inter_batch_delay[0] = min(
                    inter_batch_delay[0] * _RATE_INCREASE,
                    _MAX_INTER_BATCH,
                )
                logger.warning(
                    f"[INDEX] FloodWait detected — inter-batch delay "
                    f"→ {inter_batch_delay[0]:.1f}s"
                )
            else:
                # Clean batch → gradually speed back up
                inter_batch_delay[0] = max(
                    inter_batch_delay[0] * _RATE_DECAY,
                    MIN_INTER_BATCH,
                )
            prev_fw_count = floodwait_count[0]

            # ── Merge results ─────────────────────────────────
            group_docs    = []
            group_checked = 0
            group_skipped = 0

            for docs, n_checked, n_skipped in results:
                group_docs    += docs
                group_checked += n_checked
                group_skipped += n_skipped

            total_checked += group_checked
            total_skipped += group_skipped
            batch_count   += len(group)

            last_window_end = group[-1][-1]
            current = last_window_end + 1

            doc_buffer += group_docs

            # ── Bulk save when buffer is large enough ─────────
            is_last_group = (group_start + CONCURRENT_FETCHES) >= total_windows
            if len(doc_buffer) >= SAVE_BATCH_SIZE or (is_last_group and doc_buffer):
                to_save, n_dup = await batch_dedup_check(doc_buffer)
                total_duplicate += n_dup

                if to_save:
                    saved = await batch_save_file_docs(to_save, target=target)
                    total_saved += saved

                    for doc in to_save:
                        logger.info(
                            f"[BULK INDEX] ✅ Saved │ "
                            f"Series: {doc['normalized_name']!r} │ "
                            f"S{doc['season']:02d}E{doc['episode']:02d} │ "
                            f"{doc['quality']} │ {doc['language']} │ "
                            f"File: {doc['file_name']}"
                        )

                doc_buffer.clear()

            # ── Checkpoint every group ────────────────────────
            if current != last_checkpoint:
                await save_bot_state(chat_key, current)
                last_checkpoint = current

            # ── Console log ───────────────────────────────────
            if batch_count % max(CONCURRENT_FETCHES * 2, 6) == 0:
                pct = ((current - start_id) / max(last_id - start_id, 1)) * 100
                logger.info(
                    f"[INDEX] batch={batch_count} checked={total_checked} "
                    f"saved={total_saved} dup={total_duplicate} "
                    f"skip={total_skipped} {pct:.1f}% id={current}/{last_id} "
                    f"delay={inter_batch_delay[0]:.2f}s"
                )

            # ── Throttled Telegram progress edit ─────────────
            await push_progress()

            # ── Adaptive inter-batch pause ────────────────────
            # This is the key: we always sleep between groups,
            # and the delay grows after FloodWaits, shrinks when clean.
            await asyncio.sleep(inter_batch_delay[0])

            # ── Yield event loop ──────────────────────────────
            if batch_count % (CONCURRENT_FETCHES * 6) == 0:
                gc.collect()

        # ── Flush any remaining buffer ────────────────────────
        if doc_buffer:
            to_save, n_dup = await batch_dedup_check(doc_buffer)
            total_duplicate += n_dup
            if to_save:
                saved = await batch_save_file_docs(to_save, target=target)
                total_saved += saved
            doc_buffer.clear()

        # ── Done ──────────────────────────────────────────────
        await save_bot_state(chat_key, None)

        final_text = (
            f"✅ **Index Complete!**\n\n"
            f"📦 **Batched**   : `{total_checked:,}` msgs  (`{batch_count}` batches)\n"
            f"✅ **Saved**     : `{total_saved:,}` files\n"
            f"⏭ **Skipped**   : `{total_skipped:,}`\n"
            f"🔁 **Duplicate** : `{total_duplicate:,}`\n"
        )
        if floodwait_count[0]:
            final_text += f"⏸ **FloodWait**  : `{floodwait_count[0]}` times\n"
        final_text += (
            f"\n🎯 **Target** : `{target_label}`\n"
            f"🚀 **Mode**   : `{CONCURRENT_FETCHES}×` parallel · `{STAGGER_DELAY}s` stagger"
        )

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

        # Flush buffer on cancel so no work is lost
        if doc_buffer:
            try:
                to_save, n_dup = await batch_dedup_check(doc_buffer)
                total_duplicate += n_dup
                if to_save:
                    saved = await batch_save_file_docs(to_save, target=target)
                    total_saved += saved
            except Exception:
                pass

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
        set_caption_lang_cache(None)   # clear cache when done
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
            f"🚀 **Parallel mode**: `{CONCURRENT_FETCHES}` concurrent fetchers\n"
            f"Batch: **{FETCH_BATCH_SIZE}** msgs/call\n\n"
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
            f"🚀 **Parallel mode**: `{CONCURRENT_FETCHES}` concurrent fetchers\n"
            f"Batch: **{FETCH_BATCH_SIZE}** msgs/call\n\n"
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
        # Effective time estimate: parallel speedup
        est_groups = (n_batches + CONCURRENT_FETCHES - 1) // CONCURRENT_FETCHES

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
                f"**Confirm Parallel Batch Index**\n\n"
                f"Channel  : `{chat_id}`\n"
                f"Range    : **{start_id}** → **{last_id}** ({total_ids:,} IDs)\n"
                f"Target   : **{label}**\n"
                f"Batches  : ~**{n_batches}** × {FETCH_BATCH_SIZE} msgs\n"
                f"Fetchers : **{CONCURRENT_FETCHES}** parallel\n"
                f"Groups   : ~**{est_groups}** (effective serial steps)\n\n"
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
        logger.info(f"[INDEX] Starting parallel index — chat={chat_id} {start_id}→{last_id} target={target}")

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
                f"⏳ **Starting parallel index...**\nTarget: **{label}**",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        status_msg = await safe_send(
            bot,
            query.from_user.id,
            f"🚀 **Parallel Batch Index Starting**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{start_id}** → **{last_id}**\n"
            f"Target: **{label}**\n"
            f"Fetchers: **{CONCURRENT_FETCHES}** parallel\n\n"
            f"_Progress updates every 30 seconds..._",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )

        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, start_id, last_id, target=target)
        )
        logger.info(f"[INDEX] Parallel task created — chat={chat_id} target={target}")

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
        if index_task and not index_task.done():
            await query.answer("⚠️ Index is already running!", show_alert=True)
            return
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
                f"▶️ **Resuming Parallel Index...**\n"
                f"Channel: `{chat_id}`\n"
                f"Range: **{start_msg_id}** → **{last_msg_id}**\n"
                f"Target: **{label}**\n"
                f"Fetchers: **{CONCURRENT_FETCHES}** parallel\n\n"
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
