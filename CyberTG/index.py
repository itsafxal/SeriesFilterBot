"""
index.py — BATCH EDITION (NO SLEEP / NO EDIT SPAM)
=====================================================
BATCH INDEXING DESIGN:
  1. Collects a window of up to FETCH_BATCH_SIZE (200) message IDs.
  2. Calls bot.get_messages(chat_id, list_of_ids) — one Telegram API call
     that returns all 200 messages at once (Telegram's native batch endpoint).
  3. Parses every media message in the batch (pure CPU, zero I/O).
  4. Bulk-saves all parsed docs to SQLite (one executemany) and MongoDB
     (one bulk_write per DB) — massive reduction in round-trips.
  5. NO fixed sleep between batches — runs as fast as Telegram allows.
     If Telegram sends a FloodWait, fetch_batch retries automatically after
     the required wait time. Zero unnecessary delays otherwise.

  Status message: updated ONLY via logger every batch. The Telegram message
  is edited only at start, on FloodWait, and at completion/cancel.
  This eliminates edit errors entirely.
"""

import asyncio
import gc
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

FETCH_BATCH_SIZE  = 200    # IDs per get_messages() call (Telegram max)
LOG_EVERY         = 5      # log progress every N batches (no Telegram edits)
YIELD_EVERY       = 10     # asyncio.sleep(0) every N batches (keeps event loop alive)

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
    Prevents edit errors entirely — no exception needed.
    """
    if msg is None:
        return
    mid = msg.id
    if _last_edit_text.get(mid) == text:
        return  # identical — skip, zero API call, zero error
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
# BATCH FETCH  — get up to 200 messages in a single API call
# ─────────────────────────────────────────────────────────────

async def fetch_batch(bot: Client, chat_id: int, ids: list[int]) -> list:
    """
    Fetch a list of message IDs in one Telegram API call.
    Retries automatically on FloodWait.
    Returns list of Message objects (may include None for missing IDs).
    """
    while True:
        try:
            msgs = await bot.get_messages(chat_id, ids)
            # get_messages with a list always returns a list
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
# CORE INDEX ENGINE — BATCH VERSION (NO SLEEP)
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

    # Edit status message ONCE at start — no repeated edits during run
    await safe_edit(
        status_msg,
        f"⚡ **Batch Index Running**\n"
        f"From: `{current}` → `{last_id}`\n"
        f"Batch: **{FETCH_BATCH_SIZE}** msgs/call  |  No sleep\n"
        f"Target: **{target_label}**\n\n"
        f"Progress logged to console. Final count sent on completion.",
        parse_mode=enums.ParseMode.MARKDOWN,
    )

    await set_global_setting(
        INDEX_STATUS_KEY,
        f"RUNNING|{chat_id}|{start_id}|{last_id}|{target}",
    )

    total_checked = 0
    total_saved   = 0
    batch_count   = 0

    try:
        while current <= last_id:

            # ── Build next window of IDs ──────────────────────
            window_end = min(current + FETCH_BATCH_SIZE - 1, last_id)
            id_window  = list(range(current, window_end + 1))

            # ── Fetch batch from Telegram (1 API call) ────────
            messages = await fetch_batch(bot, chat_id, id_window)

            # ── Parse all media messages in batch ─────────────
            docs = []
            for msg in messages:
                if msg is None or msg.empty:
                    continue
                if not (msg.document or msg.video or msg.audio):
                    continue
                doc = await parse_message_to_doc(msg)
                if doc is not None:
                    docs.append(doc)

            total_checked += len(id_window)

            # ── Bulk save to Mongo + SQLite ───────────────────
            if docs:
                saved_count = await batch_save_file_docs(docs, target=target)
                total_saved += saved_count
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

            # ── Live progress every N batches ─────────────────
            # safe_edit deduplicates — only fires when text changes
            if batch_count % LOG_EVERY == 0:
                pct = ((current - start_id) / max(last_id - start_id, 1)) * 100
                progress_text = (
                    f"⚡ **Indexing...**\n"
                    f"Checked: **{total_checked:,}** msgs\n"
                    f"Saved: **{total_saved:,}** files\n"
                    f"Progress: **{pct:.1f}%**  (ID {current}/{last_id})\n"
                    f"Target: **{target_label}**"
                )
                await safe_edit(
                    status_msg, progress_text, parse_mode=enums.ParseMode.MARKDOWN
                )
                logger.info(
                    f"[INDEX] batch={batch_count} checked={total_checked} "
                    f"saved={total_saved} {pct:.1f}% id={current}/{last_id}"
                )

            # ── Yield event loop every N batches ─────────────
            if batch_count % YIELD_EVERY == 0:
                await asyncio.sleep(0)
                gc.collect()

            # No sleep — run full speed, FloodWait handled in fetch_batch

        # ── Done — edit message ONCE with final count ─────────
        await save_bot_state(chat_key, None)
        await safe_edit(
            status_msg,
            f"✅ **Index Complete!**\n"
            f"Checked: **{total_checked:,}** messages\n"
            f"Saved: **{total_saved:,}** files\n"
            f"Batches: **{batch_count}**\n"
            f"Target: **{target_label}**",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        logger.info(
            f"[INDEX] ✅ Done — checked={total_checked} saved={total_saved} "
            f"batches={batch_count} target={target}"
        )

    except asyncio.CancelledError:
        await save_bot_state(chat_key, current)
        await safe_edit(
            status_msg,
            f"⛔ **Index Cancelled**\n"
            f"Stopped at ID: **{current}**\n"
            f"Saved so far: **{total_saved:,}** files",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        logger.info(f"[INDEX] Cancelled at id={current} saved={total_saved}")

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

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⛔ Cancel Indexing", callback_data="cancel_index")
        ]])

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
            f"⚡ **Batch Index Started**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{start_id}** → **{last_id}**\n"
            f"Target: **{label}**\n"
            f"Batch: **{FETCH_BATCH_SIZE}** msgs/call  |  Full speed",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, start_id, last_id, target=target)
        )
        logger.info(f"[INDEX] Batch task created — chat={chat_id} target={target}")

    # ── CANCEL INDEX ──────────────────────────────────────────
    @cyber.on_callback_query(filters.regex("cancel_index"))
    async def cancel(bot, query):
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("Cancelling...")
        else:
            await query.message.edit_text("No active index to cancel.")
            await query.answer("Nothing running.")

    # ── CANCEL PROMPT ─────────────────────────────────────────
    @cyber.on_callback_query(filters.regex("cancel_prompt"))
    async def cancel_prompt(bot, query):
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer("Cancelled.")

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
        start_index, cancel, cancel_prompt, setskip, getskip,
    ]
