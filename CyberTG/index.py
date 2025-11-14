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

# Tune these depending on how often you want UI updates & database checkpoints
STATUS_UPDATE_EVERY = 800  # number of messages checked before a status edit
CHECKPOINT_EVERY = 800  # number of messages checked before saving checkpoint (should match or be multiple)
YIELD_EVERY = 500  # yield to event loop to reduce memory pressure

# -----------------
# Safe wrappers for Pyrogram operations
# -----------------

async def safe_sleep_for_floodwait(exc: FloodWait):
    wait = int(getattr(exc, "value", 10))
    logger.warning("FloodWait detected - sleeping for %s seconds", wait)
    await asyncio.sleep(wait)


async def safe_get_messages(bot: Client, chat_id: int, message_ids: int):
    """Resilient wrapper around bot.get_messages with FloodWait retry."""
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
    """Robust edit that will retry on FloodWait and swallow non-fatal errors."""
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
                logger.error("Giving up on editing message after 3 attempts: %s", e)
                return None
            await asyncio.sleep(1)


# -----------------
# Helper to persist and report status
# -----------------

async def update_status_message(msg_obj: Message, checked: int, saved: int, skipped_duplicates: int = 0, skipped_movies: int = 0, total_series: int = 0):
    text = f"""
Total messages checked: **{checked}**
Total series added: **{total_series}**
Duplicate skipped: **{skipped_duplicates}**
Movies skipped: **{skipped_movies}**
Total files saved: **{saved}**
"""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel Indexing", callback_data="cancel_index")]])
    return await safe_edit_message(msg_obj, text, reply_markup=kb, parse_mode=enums.ParseMode.MARKDOWN)


# -----------------
# Core indexing background task
# -----------------

async def index_files_task(bot: Client, status_msg: Optional[Message], chat_id: int, start_msg_id: int, last_msg_id: int):
    """Robust background indexing task that iterates sequentially and saves files."""
    global index_task

    chat_key = f"index_{chat_id}"

    # Determine resume point vs skip
    last_processed_id = await get_bot_state(chat_key)
    skip_id = await get_global_setting(INDEX_SKIP_ID_KEY, 0)

    current = max(int(last_processed_id or 0), int(start_msg_id), int(skip_id) + 1)
    total = int(last_msg_id)

    logger.info("Indexing started for %s from %s to %s (skip=%s, resume=%s)", chat_id, current, total, skip_id, last_processed_id)

    # Set running status
    await set_global_setting(INDEX_STATUS_KEY, f"RUNNING|{chat_id}|{current}|{last_msg_id}")

    saved = 0
    checked = 0
    counter = 0

    try:
        # Inform admins if resumed
        if current > int(start_msg_id) and status_msg:
            await safe_edit_message(status_msg, f"Indexing resumed for `{chat_id}` from message ID **{current}**...", parse_mode=enums.ParseMode.MARKDOWN)

        while current <= total:
            # Periodic yield to event loop to avoid memory pressure
            if current % YIELD_EVERY == 0:
                await asyncio.sleep(0)
                gc.collect()

            try:
                message = await safe_get_messages(bot, chat_id, current)
            except ChannelPrivate as e:
                logger.warning("Chat is private or bot not admin: %s : %s", chat_id, e)
                if status_msg:
                    await safe_edit_message(status_msg, f"Chat is private or bot not admin: {chat_id}\nError: {e}")
                return
            except asyncio.CancelledError:
                logger.info("Indexing task was cancelled externally at %s", current)
                if status_msg:
                    await safe_edit_message(status_msg, f"Indexing process was cancelled at ID **{current}**.")
                await save_bot_state(chat_key, current)
                return
            except Exception as e:
                logger.warning("Unexpected error fetching message %s: %s", current, e)
                message = None

            if message and (getattr(message, 'document', None) or getattr(message, 'video', None) or getattr(message, 'audio', None)):
                try:
                    saved_flag, count = await save_series_file_for_bulk_index(message)
                    if saved_flag:
                        saved += int(count or 0)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception("Error saving message %s from chat %s: %s", current, chat_id, e)

            current += 1
            checked += 1
            counter += 1

            # Periodic checkpoint and status update
            if counter >= STATUS_UPDATE_EVERY:
                try:
                    await save_bot_state(chat_key, current)
                    await set_global_setting(INDEX_STATUS_KEY, f"RUNNING|{chat_id}|{current}|{last_msg_id}")
                except Exception as e:
                    logger.warning("Failed to save checkpoint/status: %s", e)

                # Update status message (robust)
                try:
                    if status_msg:
                        await update_status_message(status_msg, current - 1, saved)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("Failed to update status message: %s", e)

                logger.info("INDEX: messages checked=%s saved=%s (chat=%s)", current - 1, saved, chat_id)
                counter = 0

        # Completed successfully
        await save_bot_state(chat_key, None)
        logger.info("Indexing complete for %s - total files saved: %s", chat_id, saved)
        if status_msg:
            await safe_edit_message(status_msg, f"🥳 **Complete!** Total **{saved}** Files Saved To Database! Indexing state cleared.")
    except asyncio.CancelledError:
        logger.info("Index task cancelled - saving checkpoint at %s", current)
        await save_bot_state(chat_key, current)
        if status_msg:
            await safe_edit_message(status_msg, f"Indexing cancelled. Checkpoint saved at ID **{current}**.")
        return
    except Exception as e:
        logger.exception("Indexing failed with exception: %s", e)
        try:
            await save_bot_state(chat_key, current)
            if status_msg:
                await safe_edit_message(status_msg, f"Error while indexing: {e}. Checkpoint saved at ID **{current}**.")
        except Exception:
            logger.exception("Failed to save checkpoint or notify user after error")
    finally:
        await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        index_task = None


# -----------------
# Handler registration
# -----------------

def register_index_handlers(cyber: Client):
    global index_task

    @cyber.on_message((filters.document | filters.video | filters.audio) & filters.channel)
    async def media_handler(_, message: Message):
        try:
            if message.chat.id in INDEX_CHANNELS and message.media:
                saved, count = await save_series_file_for_bulk_index(message)
                if saved:
                    logger.info("RTI: Indexed %s new file(s) from channel %s", count, message.chat.id)
        except Exception as e:
            logger.exception("Error in media_handler: %s", e)

    @cyber.on_message(filters.command("setskip") & filters.user(ADMIN_USERS) & filters.private)
    async def setskip_command(_, message: Message):
        try:
            if len(message.command) > 1:
                skip_id = int(message.command[1])
                if skip_id < 0:
                    skip_id = 0
                await set_global_setting(INDEX_SKIP_ID_KEY, skip_id)
                status = f"✅ **ON**: Indexing will skip all messages up to ID **{skip_id}**." if skip_id > 0 else "❌ **OFF**: Indexing skip is disabled (Starts from 2 or resume point)."
                await message.reply_text(f"⏭️ Index Skip feature is now set to: {status}\n\nSet to 0 to disable.", parse_mode=enums.ParseMode.MARKDOWN)
            else:
                current_skip = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
                status = f"Current index skip ID is: **{current_skip}**."
                await message.reply_text(f"Usage: `/setskip <message_id>` (e.g., `/setskip 1000`)\n{status}", parse_mode=enums.ParseMode.MARKDOWN)
        except ValueError:
            await message.reply_text("❌ Invalid number provided. Please use an integer for the message ID.", parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            logger.exception("Error in setskip_command: %s", e)
            await message.reply_text(f"An unexpected error occurred: `{e}`")

    # --- HANDLERS FOR INTERACTIVE INDEXING ---
    @cyber.on_message(filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter)
    async def index_files_forwarded(bot, message: Message):
        if lock.locked():
            await message.reply("Wait until the previous process completes.")
            return

        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            await message.reply_text("Please forward a message from the channel *without* forwarding as a copy.", parse_mode=enums.ParseMode.MARKDOWN)
            return

        last_msg_id = origin.message_id
        chat = origin.chat
        chat_id_input = chat.username if chat.username else chat.id

        if isinstance(chat_id_input, str):
            try:
                full_chat = await bot.get_chat(chat_id_input)
                final_chat_id = full_chat.id
            except Exception as e:
                await message.reply_text(f"Unable to resolve chat ID for {chat_id_input}. Error: `{e}`")
                return
        else:
            final_chat_id = chat_id_input

        if not str(final_chat_id).startswith("-100"):
            final_chat_id = final_chat_id if final_chat_id < 0 else int(f"-100{final_chat_id}")

        chat_key = f"index_{final_chat_id}"
        resume_id = await get_bot_state(chat_key)
        skip_id = await get_global_setting(INDEX_SKIP_ID_KEY, 0)
        start_id_base = 2
        start_id = max(resume_id or start_id_base, skip_id + 1)

        try:
            await safe_get_messages(bot, final_chat_id, last_msg_id)

            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"Proceed (Start at ID {start_id})", callback_data=f"index {final_chat_id} {start_id} {last_msg_id}"),
                    InlineKeyboardButton("Cancel", callback_data="can-index"),
                ]
            ])

            resume_info = ""
            if resume_id and resume_id >= start_id_base:
                resume_info = f"\n\n⚠️ **A previous index was interrupted. Resuming from ID {resume_id}.**"
            elif start_id > start_id_base:
                resume_info = f"\n\n⏭️ **Skipping up to ID {skip_id + 1}** (as set by /setskip)."

            await bot.send_message(message.from_user.id, f"Please confirm if you want to start indexing channel `{final_chat_id}` from message ID `{start_id}` up to the forwarded message ID `{last_msg_id}`.{resume_info}", reply_markup=kb, parse_mode=enums.ParseMode.MARKDOWN)

        except ChannelPrivate as e:
            await message.reply_text(f"Unable to start indexing: channel private or bot not admin. Error: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            logger.exception("Forward Index Check Failed: %s", e)
            await message.reply_text(f"Unable to start indexing. Error: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)

    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink"))
    async def manual_index(bot, message: Message):
        if lock.locked():
            await message.reply("Wait until the previous process completes.")
            return

        args = message.text.split()[1:]
        if not args or len(args) > 2:
            await message.reply("Invalid format. Use:\n`/indexlink <link>` (for 2 to link) \n`/indexlink <start_link> <end_link>` (to specify range)", parse_mode=enums.ParseMode.MARKDOWN)
            return

        try:
            chat_id, start_msg_id_base, last_msg_id = extract_links(args)

            chat_key = f"index_{chat_id}"
            resume_id = await get_bot_state(chat_key)
            skip_id = await get_global_setting(INDEX_SKIP_ID_KEY, 0)

            start_id = max(resume_id or start_msg_id_base, skip_id + 1)

            await safe_get_messages(bot, chat_id, last_msg_id)

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Proceed", callback_data=f"index {chat_id} {start_id} {last_msg_id}"), InlineKeyboardButton("Cancel", callback_data="can-index")],
            ])

            resume_info = ""
            if resume_id and resume_id >= start_msg_id_base:
                resume_info = f"\n\n⚠️ **A previous index was interrupted. Resuming from ID {resume_id}.**"
            elif start_id > start_msg_id_base:
                resume_info = f"\n\n⏭️ **Skipping up to ID {skip_id + 1}** (as set by /setskip)."

            await bot.send_message(message.from_user.id, f"Chat ID: `{chat_id}`\nStart Message ID: `{start_id}`\nLast Message ID: `{last_msg_id}`\nPlease confirm if you want to start indexing.{resume_info}", reply_markup=kb, parse_mode=enums.ParseMode.MARKDOWN)

        except Exception as e:
            logger.exception("Manual Index Check Failed: %s", e)
            await message.reply(f"Error on manual indexing: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)

    @cyber.on_callback_query(filters.regex(r"^index -?\d+ \d+ \d+"))
    async def start_index(bot, query):
        global index_task
        user_id = query.from_user.id

        if index_task and not index_task.done():
            await query.answer("An indexing process is already running.", show_alert=True)
            return

        # parse params
        chat_id, start_msg_id, last_msg_id = map(int, query.data.split()[1:])

        await query.message.edit_text("Starting index...")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel Indexing", callback_data="cancel_index")]])
        
        # FIX: Pass 'bot' as the first argument to safe_send_message
        status_msg = await safe_send_message(bot, user_id, f"Processing Index for `{chat_id}`...⏳\nStatus will be updated after every {STATUS_UPDATE_EVERY} messages checked. Current ID: **{start_msg_id}**", reply_markup=kb, parse_mode=enums.ParseMode.MARKDOWN)

        # create the background task and attach a done-callback
        index_task = asyncio.create_task(index_files_task(bot, status_msg, chat_id, start_msg_id, last_msg_id))

        def _on_done(t: asyncio.Task):
            try:
                t.result()
            except asyncio.CancelledError:
                logger.info("Index task finished: cancelled")
            except Exception as e:
                logger.exception("Index task crashed: %s", e)

        index_task.add_done_callback(_on_done)
        await query.answer("Indexing started in the background.")

    @cyber.on_callback_query(filters.regex(r"^cancel_index$"))
    async def cancel_indexing(bot, query):
        global index_task
        # Only allow ADMIN users to cancel
        if query.from_user.id not in ADMIN_USERS:
            await query.answer("You are not allowed to cancel this task.", show_alert=True)
            return

        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("Cancelling index...")
            logger.info("User requested cancellation of indexing: %s", query.from_user.id)
            # The task will perform checkpointing and update the status message
        else:
            try:
                await query.message.edit_text("No active indexing process to cancel.")
            except Exception:
                pass
            await query.answer("No active index.")

    @cyber.on_callback_query(filters.regex(r"^can-index$"))
    async def cancel_index_confirmation(bot, query):
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer("Indexing confirmation cancelled.")

    @cyber.on_message(filters.command(["index"]) & filters.user(ADMIN_USERS) & filters.private)
    async def index_comm(bot, update: Message):
        await update.reply(
            "To start indexing:\n\n"
            "1. **Forward the LAST file/message** of the channel (do NOT forward as a copy).\n"
            "2. **Use the link command:** `/indexlink <link>` or `/indexlink <start_link> <end_link>`\n\n"
            "The bot must be an administrator in the channel. Use `/setskip` to skip old messages."
        )


    return [
        setskip_command,
        media_handler,
        index_files_forwarded,
        manual_index,
        start_index,
        cancel_indexing,
        cancel_index_confirmation,
        index_comm,
    ]
