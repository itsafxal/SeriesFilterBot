"""
index.py — iter_messages EDITION
==================================

STRATEGY CHANGE: iter_messages instead of get_messages(id_list)
----------------------------------------------------------------
Previous approach: get_messages(chat_id, [id1, id2, ..., id200])
  → Uses GetMessages MTProto call with ID list
  → Telegram counts each ID individually against rate limit
  → 200 IDs = 200 rate-limit units → instant FloodWait on large channels

New approach: bot.get_history(chat_id, limit=100, offset_id=X)
  → Uses messages.getHistory MTProto call (offset-based pagination)
  → This is how Telegram clients fetch chat history natively
  → Telegram rate limit is PER CALL (not per message)
  → 100 messages per call, ~1 call/second = sustainable throughput

ARCHITECTURE:
  - Sequential history pages (offset-based, no parallelism needed)
  - Each page: up to 100 messages, one API call
  - Mandatory MIN_PAGE_DELAY between calls (starts 1.2s, adapts on FloodWait)
  - DB save batched every SAVE_BATCH_SIZE docs (single bulk_write)
  - Progress, cancel, resume, Yes/No restart prompt all preserved
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

PAGE_SIZE           = 100    # messages per get_history() call (Telegram max is 100)
MIN_PAGE_DELAY      = 1.2    # minimum seconds between API calls (safe for Telegram)
SAVE_BATCH_SIZE     = 500    # accumulate this many docs before one DB write
PROGRESS_EDIT_EVERY = 30     # seconds between Telegram message edits

# Adaptive rate
_RATE_INCREASE   = 3.0   # multiply delay on FloodWait
_RATE_DECAY      = 0.97  # multiply delay each clean page (slow recovery)
_MAX_PAGE_DELAY  = 120.0 # never wait more than this between pages

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
    current_id: int,
    start_id: int,
    last_id: int,
    total_fetched: int,
    total_saved: int,
    total_skipped: int,
    total_duplicate: int,
    page_count: int,
    target_label: str,
    floodwait_count: int,
    page_delay: float,
    status_line: str = "⚡ Indexing...",
) -> str:
    total_range = max(last_id - start_id, 1)
    pct = min(((start_id + total_fetched - start_id) / total_range) * 100, 100.0)
    # Better estimate: use current_id position
    pct = min(((current_id - start_id) / total_range) * 100, 100.0)

    filled = int(pct / 5)
    bar    = "█" * filled + "░" * (20 - filled)

    lines = [
        f"{status_line}",
        f"",
        f"[{bar}] **{pct:.1f}%**",
        f"ID: `{current_id}` / `{last_id}`",
        f"",
        f"📄 **Pages**     : `{page_count}` × {PAGE_SIZE} msgs",
        f"📦 **Fetched**   : `{total_fetched:,}` messages",
        f"✅ **Saved**     : `{total_saved:,}` files",
        f"⏭ **Skipped**   : `{total_skipped:,}`",
        f"🔁 **Duplicate** : `{total_duplicate:,}`",
    ]

    if floodwait_count:
        lines.append(f"⏸ **FloodWait**  : `{floodwait_count}` times")

    lines += [
        f"",
        f"⏱ **Pace**    : `{page_delay:.1f}s` / page",
        f"🎯 **Target** : `{target_label}`",
    ]

    return "\n".join(lines)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⛔ Cancel Indexing", callback_data="cancel_index")
    ]])


# ─────────────────────────────────────────────────────────────
# CORE INDEX ENGINE — iter_messages / get_history based
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

    resume_from_id = max(candidates)

    await set_global_setting(
        INDEX_STATUS_KEY,
        f"RUNNING|{chat_id}|{start_id}|{last_id}|{target}",
    )

    # Cache CAPTION_LANG once
    caption_lang_flag = await get_global_setting("CAPTION_LANG", True)
    set_caption_lang_cache(bool(caption_lang_flag))
    logger.info(f"[INDEX] CAPTION_LANG cached: {caption_lang_flag}")

    # Counters
    total_fetched   = 0
    total_saved     = 0
    total_skipped   = 0
    total_duplicate = 0
    page_count      = 0
    floodwait_count = 0
    current_id      = resume_from_id

    # Adaptive delay
    page_delay = MIN_PAGE_DELAY

    # Progress throttle
    last_edit_time = [0.0]

    async def push_progress(force: bool = False, status_line: str = "⚡ Indexing..."):
        now = time.monotonic()
        if not force and (now - last_edit_time[0]) < PROGRESS_EDIT_EVERY:
            return
        text = _build_progress_text(
            current_id=current_id,
            start_id=start_id,
            last_id=last_id,
            total_fetched=total_fetched,
            total_saved=total_saved,
            total_skipped=total_skipped,
            total_duplicate=total_duplicate,
            page_count=page_count,
            target_label=target_label,
            floodwait_count=floodwait_count,
            page_delay=page_delay,
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
    last_checkpoint = resume_from_id

    total_range = max(last_id - start_id, 1)
    logger.info(
        f"[INDEX] iter_messages mode: chat={chat_id} "
        f"from_id={resume_from_id} → {last_id} "
        f"({total_range:,} IDs), page={PAGE_SIZE}, delay={page_delay}s"
    )

    try:
        # ── offset_id: Telegram returns messages OLDER than this ID ──
        # We want messages from resume_from_id up to last_id.
        # Strategy: start at last_id and page backwards using offset_id.
        # Stop when the oldest message in the page is < resume_from_id.
        #
        # BUT we want ascending order for saving (so checkpoints are meaningful).
        # So we collect pages into a list, then reverse for processing.
        #
        # More efficient: page forward using add_offset trick — but Pyrogram's
        # get_history does not support forward direction directly.
        # Instead we use offset_id = last_id+1 and page backward naturally,
        # filtering out IDs < resume_from_id.

        offset_id = last_id + 1   # start just above last_id, page backwards

        while True:
            # ── Fetch one page ────────────────────────────────
            while True:
                try:
                    messages = await bot.get_history(
                        chat_id,
                        limit=PAGE_SIZE,
                        offset_id=offset_id,
                    )
                    break
                except FloodWait as fw:
                    floodwait_count += 1
                    page_delay = min(page_delay * _RATE_INCREASE, _MAX_PAGE_DELAY)
                    logger.warning(
                        f"[INDEX] FloodWait — page_delay → {page_delay:.1f}s"
                    )
                    await safe_floodwait_sleep(fw)
                except ChannelPrivate:
                    logger.error(f"[INDEX] Channel {chat_id} is private.")
                    raise asyncio.CancelledError
                except Exception as e:
                    logger.error(f"[INDEX] get_history error: {e}")
                    await asyncio.sleep(5)
                    break

            if not messages:
                logger.info("[INDEX] No more messages returned — index complete.")
                break

            page_count  += 1
            page_fetched = 0
            page_skipped = 0

            for msg in messages:
                if msg is None or msg.empty:
                    continue

                msg_id = msg.id

                # Skip messages outside our target range
                if msg_id > last_id:
                    continue
                if msg_id < resume_from_id:
                    # All subsequent messages will be older — stop
                    offset_id = -1  # sentinel to break outer loop
                    break

                page_fetched += 1

                if not (msg.document or msg.video or msg.audio):
                    page_skipped += 1
                    continue

                doc = await parse_message_to_doc(msg)
                if doc is not None:
                    doc_buffer.append(doc)
                else:
                    page_skipped += 1

            total_fetched += page_fetched
            total_skipped += page_skipped

            # Move offset_id to oldest message in this page
            if messages and offset_id != -1:
                oldest_id = min(m.id for m in messages if m and not m.empty)
                offset_id = oldest_id
                current_id = oldest_id
            else:
                # Hit the resume boundary — done
                current_id = resume_from_id
                break

            # Checkpoint
            await save_bot_state(chat_key, current_id)

            # ── Bulk save when buffer full ────────────────────
            if len(doc_buffer) >= SAVE_BATCH_SIZE:
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

            # ── Console log every 10 pages ────────────────────
            if page_count % 10 == 0:
                pct = min(((start_id + total_fetched) / total_range) * 100, 100.0)
                logger.info(
                    f"[INDEX] page={page_count} fetched={total_fetched} "
                    f"saved={total_saved} dup={total_duplicate} "
                    f"skip={total_skipped} delay={page_delay:.2f}s id~{current_id}"
                )

            # ── Progress edit ─────────────────────────────────
            await push_progress()

            # ── Adaptive inter-page delay ─────────────────────
            page_delay = max(page_delay * _RATE_DECAY, MIN_PAGE_DELAY)
            await asyncio.sleep(page_delay)

            # ── Yield event loop every 20 pages ──────────────
            if page_count % 20 == 0:
                gc.collect()
                await asyncio.sleep(0)

            if offset_id == -1:
                break

        # ── Flush remaining buffer ────────────────────────────
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
            f"📄 **Pages**     : `{page_count}` × {PAGE_SIZE} msgs\n"
            f"📦 **Fetched**   : `{total_fetched:,}` messages\n"
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
            f"[INDEX] ✅ Done — fetched={total_fetched} saved={total_saved} "
            f"dup={total_duplicate} skipped={total_skipped} "
            f"pages={page_count} target={target}"
        )

    except asyncio.CancelledError:
        await save_bot_state(chat_key, current_id)

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
            f"📦 **Fetched**   : `{total_fetched:,}` messages\n"
            f"✅ **Saved**     : `{total_saved:,}` files\n"
            f"⏭ **Skipped**   : `{total_skipped:,}`\n"
            f"🔁 **Duplicate** : `{total_duplicate:,}`\n"
            f"\n⏹ Stopped at ID: `{current_id}`\n"
            f"🎯 **Target** : `{target_label}`"
        )
        await safe_edit(
            status_msg, cancel_text,
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        logger.info(
            f"[INDEX] Cancelled at id={current_id} "
            f"saved={total_saved} dup={total_duplicate}"
        )

    finally:
        await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        set_caption_lang_cache(None)
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
                    f"[RTI] ✅ Saved │ {meta['normalized_name']!r} │ "
                    f"S{meta['season']:02d}E{meta['episode']:02d} │ "
                    f"{meta['quality']} │ {meta['language']} │ File: {file_name}"
                )
            else:
                file_name = getattr(file, "file_name", None) or "No_Name"
                logger.debug(f"[RTI] ⏭ Skipped: {file_name}")
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
            f"📄 `{PAGE_SIZE}` msgs/page · `{MIN_PAGE_DELAY}s` min delay\n"
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
            f"📄 `{PAGE_SIZE}` msgs/page · `{MIN_PAGE_DELAY}s` min delay\n"
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
        n_pages   = (total_ids + PAGE_SIZE - 1) // PAGE_SIZE
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
                f"Pages    : ~**{n_pages}** × {PAGE_SIZE} msgs\n"
                f"Min delay: **{MIN_PAGE_DELAY}s** / page\n\n"
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
            logger.error(f"[INDEX] start_index parse error: {e} | data={query.data!r}")
            try:
                await query.message.edit_text(f"❌ Failed to parse index data.\n`{query.data}`", parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass
            return

        label = {"db1": "DB1 only", "db2": "DB2 only", "both": "DB1 + DB2"}.get(target, target)
        logger.info(f"[INDEX] Starting — chat={chat_id} {start_id}→{last_id} target={target}")

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
            f"📄 `{PAGE_SIZE}` msgs/page · adaptive rate\n\n"
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
            await query.answer("⚠️ Index is already running!", show_alert=True)
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
        await query.answer("🗑 Discarding interrupted index.")
        try:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        except Exception:
            pass
        try:
            await query.message.edit_text(
                "🗑 **Interrupted index discarded.**\nStart a fresh index with /index or by forwarding a message.",
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
