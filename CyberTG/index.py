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
from helpers.index.helper import save_series_file_for_bulk_index
from helpers.filter.helper import extract_links

# ------------------ GLOBAL SETTINGS ------------------
lock = asyncio.Lock()
media_filter = filters.document | filters.video | filters.audio
index_task: Optional[asyncio.Task] = None

INDEX_SKIP_ID_KEY = "INDEX_SKIP_ID"
INDEX_STATUS_KEY = "CURRENT_INDEX_TASK"

STATUS_UPDATE_EVERY = 800
YIELD_EVERY = 500


# ------------------ SAFE WRAPPERS ------------------

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

            msg = await safe_get(bot, chat_id, current)

            if msg and (msg.document or msg.video or msg.audio):
                try:
                    ok, count = await save_series_file_for_bulk_index(msg)
                    if ok:
                        saved += count or 0
                except:
                    pass

            current_msg = current  
            current += 1
            checked += 1
            counter += 1

            if counter >= STATUS_UPDATE_EVERY:
                await save_bot_state(chat_key, current_msg)
                await safe_edit(
                    status_msg,
                    f"Currently checking message ID: **{current_msg}**\n"
                    f"Files Saved: **{saved}**"
                )
                counter = 0

        await save_bot_state(chat_key, None)
        await safe_edit(status_msg, f"Index Completed!\nTotal Files Saved: **{saved}**")

    except asyncio.CancelledError:
        await save_bot_state(chat_key, current)
        await safe_edit(status_msg, f"Index Cancelled at: **{current}**")

    finally:
        await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        index_task = None


# ------------------ CONFIRMATION START CALCULATOR ------------------

async def compute_effective_start(chat_id: int, default_start: int):
    skip_raw = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
    try:
        skip_id = int(skip_raw)
    except:
        skip_id = 0

    last_processed = await get_bot_state(f"index_{chat_id}")
    lp = int(last_processed) if last_processed else 0

    return max(default_start, skip_id + 1, lp)


# ------------------ HANDLERS ------------------

def register_index_handlers(cyber: Client):
    global index_task

    # ------- RTI AUTO-INDEXING -------
    @cyber.on_message(media_filter & filters.channel)
    async def rti(_, msg):
        channels = await get_index_channels()
        if msg.chat.id in channels:
            try:
                ok, cnt = await save_series_file_for_bulk_index(msg)
                if ok:
                    logger.info(f"RTI saved {cnt} files.")
            except Exception as e:
                logger.error(f"RTI error: {e}")

    # ------- FORWARDED INDEX -------
    @cyber.on_message(filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter)
    async def forwarded(bot, message):
        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            return await message.reply("Forward ORIGINAL message from channel.")

        chat_id = origin.chat.id
        last_id = origin.message_id

        effective_start = await compute_effective_start(chat_id, 2)

        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Proceed", callback_data=f"index {chat_id} {effective_start} {last_id}"),
                InlineKeyboardButton("Cancel", callback_data="cancel_prompt")
            ]]
        )

        await bot.send_message(
            message.from_user.id,
            f"Confirm indexing `{chat_id}` from **{effective_start}** → **{last_id}**",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb
        )

    # ------- /indexlink -------
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink"))
    async def indexlink(bot, message):
        args = message.text.split()[1:]
        chat_id, start_id, last_id = extract_links(args)

        effective_start = await compute_effective_start(chat_id, start_id)

        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Proceed", callback_data=f"index {chat_id} {effective_start} {last_id}"),
                InlineKeyboardButton("Cancel", callback_data="cancel_prompt")
            ]]
        )

        await bot.send_message(
            message.from_user.id,
            f"Confirm indexing `{chat_id}` from **{effective_start}** → **{last_id}**",
            reply_markup=kb
        )

    # ------- START INDEX -------
    @cyber.on_callback_query(filters.regex(r"^index -?\d+ \d+ \d+"))
    async def start_index(bot, query):
        global index_task

        if index_task and not index_task.done():
            return await query.answer("Index already running.", show_alert=True)

        _, chat_id, start_id, last_id = query.data.split()
        chat_id, start_id, last_id = int(chat_id), int(start_id), int(last_id)

        skip_raw = await get_global_setting(INDEX_SKIP_ID_KEY, None)
        try:
            preserved_skip = int(skip_raw) if skip_raw is not None else None
        except:
            preserved_skip = None

        await delete_all_index_states()

        if preserved_skip is not None:
            await set_global_setting(INDEX_SKIP_ID_KEY, preserved_skip)

        await save_bot_state(f"index_{chat_id}", start_id)

        await query.message.edit_text("Indexing started...")

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel Indexing", callback_data="cancel_index")]])

        status_msg = await safe_send(
            bot, query.from_user.id,
            f"Indexing `{chat_id}`...",
            reply_markup=kb
        )

        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, start_id, last_id)
        )

        await query.answer("Started.")

    # ------- CANCEL INDEX -------
    @cyber.on_callback_query(filters.regex("cancel_index"))
    async def cancel(bot, query):
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("Cancelling...")
        else:
            await query.message.edit_text("No active index to cancel.")
            await query.answer("Nothing running.")

    # ------- CANCEL PROMPT -------
    @cyber.on_callback_query(filters.regex("cancel_prompt"))
    async def cancel_prompt(bot, query):
        try:
            await query.message.delete()
        except:
            pass
        await query.answer("Cancelled.")

    # ------- /setskip -------
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("setskip"))
    async def setskip(bot, message):
        parts = message.text.split()
        if len(parts) < 2:
            return await message.reply("Usage: /setskip <msg_id>")

        try:
            val = int(parts[1])
        except:
            return await message.reply("Invalid ID.")

        await set_global_setting(INDEX_SKIP_ID_KEY, val)
        await message.reply(f"Skip set. Index will start from **{val + 1}**")

    # ------- /getskip -------
    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("getskip"))
    async def getskip(bot, message):
        raw = await get_global_setting(INDEX_SKIP_ID_KEY, None)
        await message.reply(f"Skip stored: **{raw!r}**")

    return [
        rti,
        forwarded,
        indexlink,
        start_index,
        cancel,
        cancel_prompt,
        setskip,
        getskip,
    ]
