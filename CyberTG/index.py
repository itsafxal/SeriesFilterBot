# ======================= index.py (FULL FIXED VERSION) ============================
import asyncio
import re
import gc
from typing import Optional
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChannelPrivate
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

from config import logger, ADMIN_USERS, INDEX_CHANNELS
from DB.database import (
    get_bot_state,
    save_bot_state,
    get_global_setting,
    set_global_setting,
    delete_all_index_states,   # <-- ADDED
)
from utils import save_series_file_for_bulk_index, extract_links

# --- CONFIGURABLE GLOBALS ---
lock = asyncio.Lock()
media_filter = filters.document | filters.video | filters.audio
index_task: Optional[asyncio.Task] = None

# Patterns / keys
link_pattern = r"https://t\.me/c/(\d+)/(\d+)"
INDEX_STATUS_KEY = "CURRENT_INDEX_TASK"
INDEX_SKIP_ID_KEY = "INDEX_SKIP_ID"

STATUS_UPDATE_EVERY = 800
CHECKPOINT_EVERY = 800
YIELD_EVERY = 500


# ================== Safe wrappers ====================

async def safe_sleep_for_floodwait(exc: FloodWait):
    wait = int(getattr(exc, "value", 10))
    logger.warning("FloodWait detected - sleeping for %s seconds", wait)
    await asyncio.sleep(wait)


async def safe_get_messages(bot: Client, chat_id: int, message_ids: int):
    while True:
        try:
            return await bot.get_messages(chat_id=chat_id, message_ids=message_ids, replies=0)
        except FloodWait as e:
            await safe_sleep_for_floodwait(e)
        except ChannelPrivate:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("safe_get_messages failed for %s:%s -> %s", chat_id, message_ids, e)
            return None


async def safe_send_message(bot: Client, chat_id: int, text: str, **kwargs):
    while True:
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except FloodWait as e:
            await safe_sleep_for_floodwait(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("safe_send_message failed: %s", e)
            return None


async def safe_edit_message(msg_obj: Message, text: str, **kwargs):
    if msg_obj is None:
        return None
    attempts = 0
    while True:
        try:
            return await msg_obj.edit_text(text, **kwargs)
        except FloodWait as e:
            await safe_sleep_for_floodwait(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            attempts += 1
            logger.warning("safe_edit_message failed attempt %s: %s", attempts, e)
            if attempts >= 3:
                logger.error("Giving up on editing message after 3 attempts")
                return None
            await asyncio.sleep(1)


# ================== Helper ====================

async def update_status_message(msg_obj, checked, saved, skipped_duplicates=0, skipped_movies=0, total_series=0):
    text = f"""
Total messages checked: **{checked}**
Total series added: **{total_series}**
Duplicate skipped: **{skipped_duplicates}**
Movies skipped: **{skipped_movies}**
Total files saved: **{saved}**
"""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel Indexing", callback_data="cancel_index")]])
    return await safe_edit_message(msg_obj, text, reply_markup=kb, parse_mode=enums.ParseMode.MARKDOWN)


# ================== INDEXING ENGINE ====================

async def index_files_task(bot: Client, status_msg: Message, chat_id: int, start_msg_id: int, last_msg_id: int):
    global index_task

    chat_key = f"index_{chat_id}"
    last_processed = await get_bot_state(chat_key)
    skip_id = await get_global_setting(INDEX_SKIP_ID_KEY, 0)

    current = max(last_processed or 0, start_msg_id, skip_id + 1)
    total = last_msg_id

    logger.info("Indexing %s from %s → %s", chat_id, current, total)

    await set_global_setting(INDEX_STATUS_KEY, f"RUNNING|{chat_id}|{current}|{last_msg_id}")

    saved = 0
    checked = 0
    counter = 0

    try:
        if current > start_msg_id and status_msg:
            await safe_edit_message(status_msg, f"Resumed at ID {current}...")

        while current <= total:
            if current % YIELD_EVERY == 0:
                await asyncio.sleep(0)
                gc.collect()

            try:
                message = await safe_get_messages(bot, chat_id, current)
            except ChannelPrivate:
                await safe_edit_message(status_msg, f"❌ Channel private or bot not admin.")
                return

            if message and (message.document or message.video or message.audio):
                try:
                    saved_flag, count = await save_series_file_for_bulk_index(message)
                    if saved_flag:
                        saved += count or 0
                except Exception as e:
                    logger.exception("Error saving message %s: %s", current, e)

            current += 1
            checked += 1
            counter += 1

            if counter >= STATUS_UPDATE_EVERY:
                await save_bot_state(chat_key, current)
                await set_global_setting(INDEX_STATUS_KEY, f"RUNNING|{chat_id}|{current}|{last_msg_id}")

                if status_msg:
                    await update_status_message(status_msg, checked, saved)

                counter = 0

        await save_bot_state(chat_key, None)
        await safe_edit_message(status_msg, f"🎉 Completed!\nTotal Saved: **{saved}**")
    except asyncio.CancelledError:
        await save_bot_state(chat_key, current)
        await safe_edit_message(status_msg, f"⛔ Cancelled at ID {current}")
    finally:
        await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        index_task = None


# ================== HANDLERS ====================

def register_index_handlers(cyber: Client):
    global index_task

    # Real-time auto-index
    @cyber.on_message((filters.document | filters.video | filters.audio) & filters.channel)
    async def media_handler(_, message):
        try:
            if message.chat.id in INDEX_CHANNELS:
                saved, count = await save_series_file_for_bulk_index(message)
                if saved:
                    logger.info("RTI Saved %s items", count)
        except Exception as e:
            logger.exception("RTI error: %s", e)

    # Forwarded message for indexing
    @cyber.on_message(filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter)
    async def index_files_forwarded(bot, message):
        if lock.locked():
            return await message.reply("Wait until previous finished.")

        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            return await message.reply("Forward original message from channel (not copy).")

        chat = origin.chat
        chat_id = chat.id
        last_msg_id = origin.message_id

        start_id = 2

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Proceed", callback_data=f"index {chat_id} {start_id} {last_msg_id}"),
             InlineKeyboardButton("Cancel", callback_data="can-index")],
        ])

        await bot.send_message(message.from_user.id,
                               f"Confirm indexing `{chat_id}` from {start_id} → {last_msg_id}",
                               reply_markup=kb)

    # Manual /indexlink
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink"))
    async def manual_index(bot, message):
        if lock.locked():
            return await message.reply("Wait until previous completed.")

        args = message.text.split()[1:]
        chat_id, start_id, last_id = extract_links(args)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Proceed", callback_data=f"index {chat_id} {start_id} {last_id}"),
             InlineKeyboardButton("Cancel", callback_data="can-index")],
        ])

        await bot.send_message(message.from_user.id,
                               f"Confirm indexing `{chat_id}` from {start_id} → {last_id}",
                               reply_markup=kb)

    # ======================= START INDEX (FULL FIX ADDED) ==========================
    @cyber.on_callback_query(filters.regex(r"^index -?\d+ \d+ \d+"))
    async def start_index(bot, query):
        global index_task
        user_id = query.from_user.id

        if index_task and not index_task.done():
            return await query.answer("Already running.", show_alert=True)

        chat_id, start_id, last_id = map(int, query.data.split()[1:])

        # ********** 🔥 FULL FIX: CLEAR OLD INDEX STATES ************
        await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        await delete_all_index_states()                  # <---- FULL FIX
        await save_bot_state(f"index_{chat_id}", start_id)
        # ***********************************************************

        await query.message.edit_text("Starting index...")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel Indexing", callback_data="cancel_index")]])

        status_msg = await safe_send_message(
            bot, user_id,
            f"Indexing `{chat_id}` from **{start_id}** → **{last_id}**\nUpdating every {STATUS_UPDATE_EVERY} messages.",
            reply_markup=kb,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        index_task = asyncio.create_task(index_files_task(bot, status_msg, chat_id, start_id, last_id))
        await query.answer("Index started.")

    # ======================= CANCEL INDEX ==========================
    @cyber.on_callback_query(filters.regex(r"^cancel_index$"))
    async def cancel_indexing(bot, query):
        global index_task
        
        if query.from_user.id not in ADMIN_USERS:
            return await query.answer("Not allowed.", show_alert=True)

        if index_task and not index_task.done():
            index_task.cancel()
            return await query.answer("Cancelling...")
        else:
            await query.message.edit_text("No active index.")
            await query.answer("Nothing to cancel.")

    @cyber.on_callback_query(filters.regex(r"^can-index$"))
    async def cancel_index_confirmation(bot, query):
        try:
            await query.message.delete()
        except:
            pass
        await query.answer("Cancelled.")

    @cyber.on_message(filters.command(["index"]) & filters.user(ADMIN_USERS) & filters.private)
    async def index_comm(bot, message):
        await message.reply("Forward last message or use /indexlink.")

        # ======================= /setskip COMMAND ==========================
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("setskip"))
    async def set_skip_cmd(bot, message):
        """
        Usage: /setskip <message_id>
        Forces indexer to skip all messages <= that ID.
        """
        args = message.text.split()
        if len(args) < 2:
            return await message.reply("Usage: `/setskip <msg_id>`", parse_mode=enums.ParseMode.MARKDOWN)

        try:
            skip_id = int(args[1])
        except ValueError:
            return await message.reply("Invalid ID. Provide a numeric message_id.")

        await set_global_setting(INDEX_SKIP_ID_KEY, skip_id)

        await message.reply(
            f"Skip ID updated.\nAll indexing will resume from **{skip_id + 1}**.",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    return [
        media_handler,
        index_files_forwarded,
        manual_index,
        start_index,
        cancel_indexing,
        cancel_index_confirmation,
        index_comm,
    ]
