"""
index.py — MULTI-DB EDITION
=============================
Changes from single-DB version:
  1. Before every index starts (forwarded + /indexlink), admin is asked:
       "Index to DB1 / DB2 / Both?"  via inline keyboard.
  2. The chosen target ("db1" | "db2" | "both") is carried through the
     confirmation flow and passed to save_series_file_for_bulk_index().
  3. RTI (real-time/live index) always writes to BOTH Mongo file DBs + SQLite.
  4. Index status key now includes the target so resume works correctly.
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
from utils.cyber_index import save_series_file_for_bulk_index
from utils.cyber_filter import extract_links

# ─────────────────────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────────────────────

lock        = asyncio.Lock()
media_filter = filters.document | filters.video | filters.audio
index_task: Optional[asyncio.Task] = None

INDEX_SKIP_ID_KEY = "INDEX_SKIP_ID"
INDEX_STATUS_KEY  = "CURRENT_INDEX_TASK"

STATUS_UPDATE_EVERY = 800
YIELD_EVERY         = 500


# ─────────────────────────────────────────────────────────────
# SAFE WRAPPERS
# ─────────────────────────────────────────────────────────────

async def safe_floodwait_sleep(exc: FloodWait):
    await asyncio.sleep(int(getattr(exc, "value", 10)))


async def safe_get(bot: Client, chat_id: int, msg_id: int):
    while True:
        try:
            return await bot.get_messages(chat_id, msg_id)
        except FloodWait as f:
            await safe_floodwait_sleep(f)
        except Exception:
            return None


async def safe_send(bot: Client, uid: int, text: str, **kwargs):
    while True:
        try:
            return await bot.send_message(uid, text, **kwargs)
        except FloodWait as f:
            await safe_floodwait_sleep(f)


async def safe_edit(msg: Message, text: str, **kwargs):
    if msg is None:
        return
    while True:
        try:
            return await msg.edit_text(text, **kwargs)
        except FloodWait as f:
            await safe_floodwait_sleep(f)
        except Exception:
            return


# ─────────────────────────────────────────────────────────────
# INDEX ENGINE
# ─────────────────────────────────────────────────────────────

async def index_files_task(
    bot: Client,
    status_msg: Message,
    chat_id: int,
    start_id: int,
    last_id: int,
    target: str = "both",          # ← NEW: "db1" | "db2" | "both"
):
    global index_task

    chat_key = f"index_{chat_id}"

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

    target_label = {"db1": "DB1 only", "db2": "DB2 only", "both": "DB1 + DB2"}.get(target, target)
    await safe_edit(
        status_msg,
        f"Starting from message ID: **{current}**\nTarget: **{target_label}**",
    )

    # Persist status so resume knows the target too
    await set_global_setting(
        INDEX_STATUS_KEY,
        f"RUNNING|{chat_id}|{start_id}|{last_id}|{target}",
    )

    checked = 0
    saved   = 0
    counter = 0

    try:
        while current <= last_id:

            if current % YIELD_EVERY == 0:
                await asyncio.sleep(0)
                gc.collect()

            msg = await safe_get(bot, chat_id, current)

            if msg and (msg.document or msg.video or msg.audio):
                try:
                    ok, count = await save_series_file_for_bulk_index(msg, target=target)
                    if ok:
                        saved += count or 0
                        from utils.cyber_index import parse_file_name
                        file_obj  = msg.video or msg.audio or msg.document
                        file_name = getattr(file_obj, "file_name", None) or "No_Name"
                        meta      = parse_file_name(file_name)
                        logger.info(
                            f"[BULK INDEX] ✅ Saved │ "
                            f"Series: {meta['normalized_name']!r} │ "
                            f"S{meta['season']:02d}E{meta['episode']:02d} │ "
                            f"{meta['quality']} │ "
                            f"{meta['language']} │ "
                            f"MsgID: {current} │ "
                            f"File: {file_name}"
                        )
                except Exception:
                    pass

            current_msg  = current
            current     += 1
            checked     += 1
            counter     += 1

            if counter >= STATUS_UPDATE_EVERY:
                await save_bot_state(chat_key, current_msg)
                await safe_edit(
                    status_msg,
                    f"Checking ID: **{current_msg}** | Saved: **{saved}** | Target: **{target_label}**",
                )
                counter = 0

        await save_bot_state(chat_key, None)
        await safe_edit(
            status_msg,
            f"✅ Index Complete!\nTotal Files Saved: **{saved}**\nTarget: **{target_label}**",
        )

    except asyncio.CancelledError:
        await save_bot_state(chat_key, current)
        await safe_edit(status_msg, f"⛔ Index Cancelled at: **{current}**")

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

    # ── RTI: real-time auto-index on new channel messages ────────────────
    # Always writes to BOTH Mongo file DBs + SQLite
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

    # ── FORWARDED message → ask DB target, then confirm ──────────────────
    @cyber.on_message(
        filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter
    )
    async def forwarded(bot, message):
        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            return await message.reply("Forward an ORIGINAL message from a channel.")

        chat_id  = origin.chat.id
        last_id  = origin.message_id
        eff_start = await compute_effective_start(chat_id, 2)

        # Step 1 — ask which DB
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 DB1",  callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db1"),
                InlineKeyboardButton("📁 DB2",  callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db2"),
            ],
            [
                InlineKeyboardButton("📁📁 Both", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both"),
            ],
            [
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_prompt"),
            ],
        ])

        await bot.send_message(
            message.from_user.id,
            f"📦 **Index Target?**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{eff_start}** → **{last_id}**\n\n"
            f"Choose which MongoDB to write files into:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    # ── /indexlink → same flow ────────────────────────────────────────────
    @cyber.on_message(
        filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink")
    )
    async def indexlink(bot, message):
        args = message.text.split()[1:]
        chat_id, start_id, last_id = extract_links(args)
        eff_start = await compute_effective_start(chat_id, start_id)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📁 DB1",  callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db1"),
                InlineKeyboardButton("📁 DB2",  callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db2"),
            ],
            [
                InlineKeyboardButton("📁📁 Both", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both"),
            ],
            [
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_prompt"),
            ],
        ])

        await bot.send_message(
            message.from_user.id,
            f"📦 **Index Target?**\n"
            f"Channel: `{chat_id}`\n"
            f"Range: **{eff_start}** → **{last_id}**\n\n"
            f"Choose which MongoDB to write files into:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    # ── DB target chosen → show final confirmation ────────────────────────
    @cyber.on_callback_query(filters.regex(r"^idx_db\|"))
    async def idx_db_chosen(bot, query):
        # format: idx_db|chat_id|start_id|last_id|target
        await query.answer()   # answer FIRST to prevent timeout spinner
        parts = query.data.split("|")
        _, chat_id, start_id, last_id, target = parts
        chat_id, start_id, last_id = int(chat_id), int(start_id), int(last_id)

        label = {"db1": "📁 DB1 only", "db2": "📁 DB2 only", "both": "📁📁 DB1 + DB2"}.get(target, target)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Proceed", callback_data=f"index|{chat_id}|{start_id}|{last_id}|{target}"),
            ],
            [
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_prompt"),
            ],
        ])

        try:
            await query.message.edit_text(
                f"**Confirm Index**\n"
                f"Channel: `{chat_id}`\n"
                f"Range: **{start_id}** → **{last_id}**\n"
                f"Target: **{label}**\n\n"
                f"Tap Proceed to start indexing.",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        except Exception as e:
            logger.error(f"[INDEX] idx_db_chosen edit failed: {e}")

    # ── START INDEX ───────────────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex(r"^index\|"))
    async def start_index(bot, query):
        global index_task

        # ✅ ALWAYS answer immediately — prevents Telegram timeout spinner
        await query.answer()

        if index_task and not index_task.done():
            await query.answer("Index already running.", show_alert=True)
            return

        # format: index|chat_id|start_id|last_id|target
        try:
            parts    = query.data.split("|")
            chat_id  = int(parts[1])
            start_id = int(parts[2])
            last_id  = int(parts[3])
            target   = parts[4]
        except Exception as e:
            logger.error(f"[INDEX] start_index parse error: {e} | data={query.data!r}")
            try:
                await query.message.edit_text(f"❌ Failed to parse index data.\n`{query.data}`",
                                              parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass
            return

        label = {"db1": "DB1 only", "db2": "DB2 only", "both": "DB1 + DB2"}.get(target, target)
        logger.info(f"[INDEX] Proceed clicked — chat={chat_id} {start_id}→{last_id} target={target}")

        try:
            await query.message.edit_text(f"⏳ Starting index...\nTarget: **{label}**",
                                          parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"[INDEX] Could not edit confirm message: {e}")

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

        status_msg = await safe_send(
            bot,
            query.from_user.id,
            f"📦 Indexing `{chat_id}`...\nTarget: **{label}**\nStarting from: **{start_id}**",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, start_id, last_id, target=target)
        )
        logger.info(f"[INDEX] Task created — chat={chat_id} target={target}")

    # ── CANCEL INDEX ──────────────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex("cancel_index"))
    async def cancel(bot, query):
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("Cancelling...")
        else:
            await query.message.edit_text("No active index to cancel.")
            await query.answer("Nothing running.")

    # ── CANCEL PROMPT ─────────────────────────────────────────────────────
    @cyber.on_callback_query(filters.regex("cancel_prompt"))
    async def cancel_prompt(bot, query):
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer("Cancelled.")

    # ── /setskip ──────────────────────────────────────────────────────────
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

    # ── /getskip ──────────────────────────────────────────────────────────
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
