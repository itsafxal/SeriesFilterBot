# ======================= index.py (CLEAN FINAL VERSION) ============================
import asyncio
import gc
from typing import Optional
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChannelPrivate
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import logger, ADMIN_USERS, INDEX_CHANNELS
from DB.database import (
    get_bot_state,
    save_bot_state,
    get_global_setting,
    set_global_setting,
    delete_all_index_states,
)
from utils import extract_links, save_series_file_for_bulk_index


# ------------------ GLOBAL SETTINGS ------------------
lock = asyncio.Lock()
media_filter = filters.document | filters.video | filters.audio
index_task: Optional[asyncio.Task] = None

INDEX_SKIP_ID_KEY = "INDEX_SKIP_ID"
INDEX_STATUS_KEY = "CURRENT_INDEX_TASK"

STATUS_UPDATE_EVERY = 800
YIELD_EVERY = 500


# ------------------ SAFE WRAPPERS ------------------

async def safe_sleep_for_floodwait(exc: FloodWait):
    await asyncio.sleep(int(getattr(exc, "value", 10)))


async def safe_get_messages(bot: Client, chat_id: int, msg_id: int):
    while True:
        try:
            return await bot.get_messages(chat_id=chat_id, message_ids=msg_id, replies=0)
        except FloodWait as f:
            await safe_sleep_for_floodwait(f)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None


async def safe_send_message(bot: Client, uid: int, text: str, **kwargs):
    while True:
        try:
            return await bot.send_message(uid, text, **kwargs)
        except FloodWait as f:
            await safe_sleep_for_floodwait(f)
        except asyncio.CancelledError:
            raise


async def safe_edit(msg: Message, text: str, **kwargs):
    if not msg:
        return
    while True:
        try:
            return await msg.edit_text(text, **kwargs)
        except FloodWait as f:
            await safe_sleep_for_floodwait(f)
        except asyncio.CancelledError:
            raise
        except Exception:
            return


# ------------------ INDEX ENGINE ------------------

async def index_files_task(bot: Client, status_msg: Message, chat_id: int, start_id: int, last_id: int):
    global index_task

    chat_key = f"index_{chat_id}"

    last_processed = await get_bot_state(chat_key)
    skip_raw = await get_global_setting(INDEX_SKIP_ID_KEY, 0)

    try:
        skip_id = int(skip_raw)
    except:
        skip_id = 0

    candidates = [start_id, skip_id + 1]
    if last_processed:
        try:
            candidates.append(int(last_processed))
        except:
            pass

    current = max(candidates)

    await safe_edit(status_msg, f"Starting from message ID: **{current}**")

    checked = 0
    saved = 0
    counter = 0

    try:
        while current <= last_id:

            if current % YIELD_EVERY == 0:
                await asyncio.sleep(0)
                gc.collect()

            msg = await safe_get_messages(bot, chat_id, current)

            if msg and (msg.document or msg.video or msg.audio):
                try:
                    saved_flag, count = await save_series_file_for_bulk_index(msg)
                    if saved_flag:
                        saved += count or 0
                except Exception as e:
                    logger.error(f"Save error at {current}: {e}")

            current_msg_id = current  # display before incrementation

            current += 1
            checked += 1
            counter += 1

            if counter >= STATUS_UPDATE_EVERY:
                await save_bot_state(chat_key, current_msg_id)
                await safe_edit(
                    status_msg,
                    f"Currently checking message ID: **{current_msg_id}**\n"
                    f"Files Saved: **{saved}**"
                )
                counter = 0

        await save_bot_state(chat_key, None)
        await safe_edit(status_msg, f"Indexing Complete!\nTotal Files Saved: **{saved}**")

    except asyncio.CancelledError:
        await save_bot_state(chat_key, current)
        await safe_edit(status_msg, f"Indexing Cancelled at message ID: **{current}**")

    finally:
        await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        index_task = None


# ------------------ HANDLERS ------------------

def register_index_handlers(cyber: Client):
    global index_task

    # Real-Time Auto Index (for allowed channels)
    @cyber.on_message(media_filter & filters.channel)
    async def rti_handler(_, msg):
        if msg.chat.id in INDEX_CHANNELS:
            try:
                saved, count = await save_series_file_for_bulk_index(msg)
                if saved:
                    logger.info(f"RTI Saved {count} files")
            except Exception as e:
                logger.error(f"RTI error: {e}")

    # Forwarded message indexing
    @cyber.on_message(filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter)
    async def forward_index(bot, message):
        if lock.locked():
            return await message.reply("Previous indexing still running.")

        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            return await message.reply("Forward original message from channel.")

        chat_id = origin.chat.id
        last_id = origin.message_id
        start_id = 2

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Proceed", callback_data=f"index {chat_id} {start_id} {last_id}"),
                InlineKeyboardButton("Cancel", callback_data="cancel_prompt")
            ]
        ])

        await bot.send_message(
            message.from_user.id,
            f"Confirm indexing `{chat_id}` from {start_id} → {last_id}",
            reply_markup=kb,
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # /indexlink command
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink"))
    async def indexlink(bot, message):
        if lock.locked():
            return await message.reply("Previous indexing still running.")

        args = message.text.split()[1:]
        chat_id, start_id, last_id = extract_links(args)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Proceed", callback_data=f"index {chat_id} {start_id} {last_id}"),
                InlineKeyboardButton("Cancel", callback_data="cancel_prompt")
            ]
        ])

        await bot.send_message(
            message.from_user.id,
            f"Confirm indexing `{chat_id}` {start_id} → {last_id}",
            reply_markup=kb
        )

    # Start Index
    @cyber.on_callback_query(filters.regex(r"^index -?\d+ \d+ \d+"))
    async def start_index(bot, query):
        global index_task

        if index_task and not index_task.done():
            return await query.answer("Index already running.", show_alert=True)

        chat_id, start_id, last_id = map(int, query.data.split()[1:])

        raw_skip = await get_global_setting(INDEX_SKIP_ID_KEY, None)
        try:
            preserved_skip = int(raw_skip) if raw_skip is not None else None
        except:
            preserved_skip = None

        await delete_all_index_states()

        if preserved_skip is not None:
            await set_global_setting(INDEX_SKIP_ID_KEY, preserved_skip)

        await save_bot_state(f"index_{chat_id}", start_id)

        await query.message.edit_text("Indexing started...")

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel Indexing", callback_data="cancel_index")]])

        status_msg = await safe_send_message(
            bot, query.from_user.id,
            f"Indexing `{chat_id}`...",
            reply_markup=kb
        )

        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, start_id, last_id)
        )

        await query.answer("Index started.")

    # Cancel index
    @cyber.on_callback_query(filters.regex("cancel_index"))
    async def cancel(bot, query):
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("Cancelling...")
        else:
            await query.message.edit_text("No active index.")
            await query.answer("Nothing to cancel.")

    # Cancel prompt
    @cyber.on_callback_query(filters.regex("cancel_prompt"))
    async def cancel_prompt(bot, query):
        try:
            await query.message.delete()
        except:
            pass
        await query.answer("Cancelled.")

    # /setskip
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("setskip"))
    async def setskip(bot, message):
        parts = message.text.split()
        if len(parts) < 2:
            return await message.reply("Usage: /setskip <msg_id>")

        try:
            skip_val = int(parts[1])
        except:
            return await message.reply("Invalid number.")

        await set_global_setting(INDEX_SKIP_ID_KEY, skip_val)
        await message.reply(f"Skip set. Index will start from {skip_val + 1}")

    # /getskip
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("getskip"))
    async def getskip(bot, message):
        raw = await get_global_setting(INDEX_SKIP_ID_KEY, None)
        await message.reply(f"Skip stored: {raw!r}")

    return [
        rti_handler,
        forward_index,
        indexlink,
        start_index,
        cancel,
        cancel_prompt,
        setskip,
        getskip,
    ]
