import asyncio
import gc
import time
from typing import Optional

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChannelPrivate
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import logger, ADMIN_USERS
from DB.database import (
    get_bot_state, save_bot_state,
    get_global_setting, set_global_setting,
    delete_all_index_states, get_index_channels,
)
from utils.cyber_index import (
    parse_message_to_doc, batch_save_file_docs,
    save_series_file_for_bulk_index,
    set_caption_lang_cache, batch_dedup_check,
    set_clean_keywords_cache,
)
from utils.cyber_filter import extract_links

# ---------------------------------------------------------------
# TUNING  (increase CONCURRENT_FETCHES carefully if needed)
# ---------------------------------------------------------------

FETCH_BATCH        = 200   # IDs per get_messages() call (Telegram max)
CONCURRENT_FETCHES = 1     # bot token: 1 concurrent fetch only — parallel calls trigger FloodWait
SAVE_BATCH_SIZE    = 800   # parsed docs buffer before DB flush
CHECKPOINT_EVERY   = 25    # checkpoint every N rounds
PROGRESS_EVERY     = 15    # seconds between Telegram message edits
ROUND_COOLDOWN     = 3.5   # 1 call per 3.5s ≈ 17 calls/min — safely under bot's ~20/min limit

INDEX_SKIP_KEY   = "INDEX_SKIP_ID"
INDEX_STATUS_KEY = "CURRENT_INDEX_TASK"

media_filter = filters.document | filters.video | filters.audio
index_task: Optional[asyncio.Task] = None
lock = asyncio.Lock()


# ---------------------------------------------------------------
# TELEGRAM WRAPPERS
# ---------------------------------------------------------------

async def _fw_sleep(exc: FloodWait):
    wait = max(int(getattr(exc, "value", 10)), 5)
    logger.warning(f"[INDEX] FloodWait {wait}s - sleeping")
    await asyncio.sleep(wait + 2)


async def safe_send(bot: Client, uid: int, text: str, **kw) -> Optional[Message]:
    while True:
        try:
            return await bot.send_message(uid, text, **kw)
        except FloodWait as f:
            await _fw_sleep(f)
        except Exception as e:
            logger.error(f"[INDEX] safe_send: {e}")
            return None


_last_text: dict[int, str] = {}


async def safe_edit(msg: Message, text: str, **kw):
    if msg is None or _last_text.get(msg.id) == text:
        return
    try:
        await msg.edit_text(text, **kw)
        _last_text[msg.id] = text
    except FloodWait as f:
        await _fw_sleep(f)
        try:
            await msg.edit_text(text, **kw)
            _last_text[msg.id] = text
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[INDEX] safe_edit: {e}")


def _cancel_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Stop Indexing", callback_data="cancel_index")
    ]])


# ---------------------------------------------------------------
# PROGRESS
# ---------------------------------------------------------------

def _progress(*, cid, start_id, last_id, checked, saved, skipped, dup,
               calls, fw_count, mps, status="Indexing...") -> str:
    span   = max(last_id - start_id, 1)
    id_pct = min((cid - start_id) / span * 100, 100.0)
    bar    = chr(9608) * int(id_pct / 5) + chr(9617) * (20 - int(id_pct / 5))

    eta = ""
    remaining_ids = max(last_id - cid, 0)
    if mps > 0 and remaining_ids > 0 and checked > 0:
        ids_covered    = max(cid - start_id, 1)
        density        = ids_covered / max(checked, 1)
        remaining_msgs = remaining_ids / density
        s = int(remaining_msgs / mps)
        if s < 60:
            eta = f"  ETA ~{s}s"
        elif s < 3600:
            eta = f"  ETA ~{s // 60}m {s % 60}s"
        else:
            eta = f"  ETA ~{s // 3600}h {s % 3600 // 60}m"

    lines = [
        f"{status}", "",
        f"[{bar}] **{id_pct:.1f}%**{eta}",
        f"ID `{cid:,}` / `{last_id:,}`", "",
        f"Checked   : `{checked:,}`  (`{calls:,}` calls)",
        f"Saved     : `{saved:,}`",
        f"Skipped   : `{skipped:,}`",
        f"Duplicate : `{dup:,}`",
        f"Speed     : `{mps:.0f}` msg/s",
        f"Fetching  : `{CONCURRENT_FETCHES} x {FETCH_BATCH}` IDs/round",
    ]
    if fw_count:
        lines.append(f"FloodWait : `{fw_count}` times")
    return "\n".join(lines)


# ---------------------------------------------------------------
# FETCH  (concurrent)
# ---------------------------------------------------------------

async def _fetch_one(bot: Client, chat_id: int, id_window: list, fw_count: list) -> list:
    while True:
        try:
            msgs = await bot.get_messages(chat_id, id_window)
            return msgs if isinstance(msgs, list) else [msgs]
        except FloodWait as fw:
            fw_count[0] += 1
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[BOT] FloodWait {wait}s - retrying")
            # Extra buffer: wait the full required time plus 20% headroom
            await asyncio.sleep(wait * 1.2 + 1)
        except ChannelPrivate:
            logger.error(f"[BOT] Channel {chat_id} is private/inaccessible")
            return []
        except Exception as e:
            logger.error(f"[BOT] get_messages error: {e}")
            await asyncio.sleep(2)
            return []


async def _fetch_round(
    bot: Client,
    chat_id: int,
    windows: list,
    fw_count: list,
) -> list:
    """Single sequential fetch — bot token cannot safely run parallel get_messages."""
    flat = []
    for w in windows:
        msgs = await _fetch_one(bot, chat_id, w, fw_count)
        flat.extend(msgs)
    return flat


# ---------------------------------------------------------------
# DB FLUSH
# ---------------------------------------------------------------

async def _flush(buf: list, target: str) -> tuple:
    if not buf:
        return 0, 0
    to_save, n_dup = await batch_dedup_check(buf)
    saved = 0
    if to_save:
        saved = await batch_save_file_docs(to_save, target=target)
        for doc in to_save:
            logger.info(
                f"[IDX] {doc['normalized_name']!r} "
                f"S{doc['season']:02d}E{doc['episode']:02d} "
                f"{doc['quality']} {doc['language']}"
            )
    return saved, n_dup


# ---------------------------------------------------------------
# CORE TASK
# ---------------------------------------------------------------

async def index_files_task(
    bot: Client, status_msg: Message,
    chat_id: int, start_id: int, last_id: int, target: str = "both",
):
    global index_task

    chat_key     = f"index_{chat_id}"
    target_label = {"db1": "DB1", "db2": "DB2", "both": "DB1+DB2"}.get(target, target)

    last_cp  = await get_bot_state(chat_key)
    skip_raw = await get_global_setting(INDEX_SKIP_KEY, 0)
    try:
        skip_id = int(skip_raw) if skip_raw else 0
    except Exception:
        skip_id = 0

    candidates = [start_id, skip_id + 1]
    if last_cp:
        try:
            candidates.append(int(last_cp))
        except Exception:
            pass
    start_from = max(candidates)

    await set_global_setting(INDEX_STATUS_KEY, f"RUNNING|{chat_id}|{start_id}|{last_id}|{target}")

    cl_flag = await get_global_setting("CAPTION_LANG", True)
    set_caption_lang_cache(bool(cl_flag))
    from DB.database import get_caption_clean_keywords as _get_kw
    set_clean_keywords_cache(await _get_kw())

    logger.info(
        f"[INDEX] START  chat={chat_id}  {start_from}>{last_id}  "
        f"target={target}  mode={CONCURRENT_FETCHES}x{FETCH_BATCH}"
    )

    checked = saved = skipped = dup = calls = 0
    fw_count  = [0]
    t0        = time.monotonic()
    last_edit = [0.0]
    cur_id    = [start_from]
    current   = start_from
    buf       = []
    cp_n      = 0

    def mps():
        return checked / max(time.monotonic() - t0, 0.001)

    async def push(force=False, status="Indexing..."):
        now = time.monotonic()
        if not force and now - last_edit[0] < PROGRESS_EVERY:
            return
        await safe_edit(
            status_msg,
            _progress(
                cid=cur_id[0], start_id=start_id, last_id=last_id,
                checked=checked, saved=saved, skipped=skipped, dup=dup,
                calls=calls, fw_count=fw_count[0], mps=mps(), status=status,
            ),
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        last_edit[0] = time.monotonic()

    async def _proc(msg) -> Optional[dict]:
        if msg is None or msg.empty:
            return None
        if not (msg.document or msg.video or msg.audio):
            return None
        return await parse_message_to_doc(msg)

    await push(force=True)

    async with lock:
        try:
            while current <= last_id:
                end = min(current + FETCH_BATCH - 1, last_id)
                window = list(range(current, end + 1))
                current = end + 1
                cur_id[0] = current - 1

                msgs = await _fetch_round(bot, chat_id, [window], fw_count)
                calls += 1

                # Fixed cooldown — 3.5s keeps bot safely under ~20 calls/min limit
                await asyncio.sleep(ROUND_COOLDOWN)

                for msg in msgs:
                    checked += 1
                    if not msg or msg.empty:
                        skipped += 1
                        continue
                    if not (msg.document or msg.video or msg.audio):
                        skipped += 1
                        continue
                    doc = await _proc(msg)
                    if doc:
                        buf.append(doc)
                    else:
                        skipped += 1

                if len(buf) >= SAVE_BATCH_SIZE:
                    sv, nd = await _flush(list(buf), target)
                    saved += sv; dup += nd
                    buf.clear()

                cp_n += 1
                if cp_n >= CHECKPOINT_EVERY:
                    await save_bot_state(chat_key, cur_id[0])
                    cp_n = 0
                    gc.collect()

                await push()

            if buf:
                sv, nd = await _flush(list(buf), target)
                saved += sv; dup += nd
                buf.clear()

            await save_bot_state(chat_key, None)
            avg = int(mps())

            final = (
                f"Index Complete!\n\n"
                f"Target    : `{target_label}`\n"
                f"Mode      : `{CONCURRENT_FETCHES} x {FETCH_BATCH}` IDs/round\n"
                f"Checked   : `{checked:,}`\n"
                f"Saved     : `{saved:,}` files\n"
                f"Skipped   : `{skipped:,}`\n"
                f"Duplicate : `{dup:,}`\n"
                f"API calls : `{calls:,}`\n"
                f"Avg speed : `{avg:,}` msg/s\n"
            )
            if fw_count[0]:
                final += f"FloodWait : `{fw_count[0]}` times\n"

            await safe_edit(status_msg, final, parse_mode=enums.ParseMode.MARKDOWN)
            logger.info(
                f"[INDEX] done  checked={checked}  saved={saved}  "
                f"dup={dup}  skip={skipped}  calls={calls}  speed={avg}msg/s"
            )

        except asyncio.CancelledError:
            await save_bot_state(chat_key, cur_id[0])
            if buf:
                try:
                    sv, nd = await _flush(buf, target)
                    saved += sv; dup += nd
                except Exception:
                    pass
            await safe_edit(
                status_msg,
                f"Index Cancelled\n\n"
                f"Checked   : `{checked:,}`\n"
                f"Saved     : `{saved:,}` files\n"
                f"Skipped   : `{skipped:,}`\n"
                f"Duplicate : `{dup:,}`\n"
                f"Stopped   : ID `{cur_id[0]}`\n"
                f"Target    : `{target_label}`",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            logger.info(f"[INDEX] cancelled at id={cur_id[0]} saved={saved}")

        finally:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
            set_caption_lang_cache(None)
            set_clean_keywords_cache(None)
            index_task = None


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------

async def compute_effective_start(chat_id: int, default_start: int) -> int:
    skip_raw = await get_global_setting(INDEX_SKIP_KEY, 0)
    try:
        skip_id = int(skip_raw) if skip_raw else 0
    except Exception:
        skip_id = 0
    lp = await get_bot_state(f"index_{chat_id}")
    return max(default_start, skip_id + 1, int(lp) if lp else 0)


def _mode_badge() -> str:
    return f"Bot mode  `{CONCURRENT_FETCHES} x {FETCH_BATCH}` IDs/round"


async def _est(start_id: int, last_id: int) -> str:
    total         = max(last_id - start_id + 1, 1)
    ids_per_round = CONCURRENT_FETCHES * FETCH_BATCH
    rounds        = max(total // ids_per_round, 1)
    est_sec       = int(rounds * 0.4)
    if est_sec < 60:
        return f"~{est_sec}s"
    elif est_sec < 3600:
        return f"~{est_sec // 60}m {est_sec % 60}s"
    else:
        return f"~{est_sec // 3600}h {est_sec % 3600 // 60}m"


# ---------------------------------------------------------------
# HANDLERS
# ---------------------------------------------------------------

def register_index_handlers(cyber: Client):
    global index_task

    @cyber.on_message(media_filter & filters.channel)
    async def rti(_, msg):
        channels = await get_index_channels()
        if msg.chat.id not in channels:
            return
        try:
            file = msg.document or msg.video or msg.audio
            if not file:
                return
            ok, _ = await save_series_file_for_bulk_index(msg, target="mongo")
            fn    = getattr(file, "file_name", None) or "No_Name"
            if ok:
                from utils.cyber_index import parse_file_name
                m = parse_file_name(fn)
                logger.info(
                    f"[RTI] {m['normalized_name']!r} "
                    f"S{m['season']:02d}E{m['episode']:02d} "
                    f"{m['quality']} {m['language']} | {fn}"
                )
            else:
                logger.debug(f"[RTI] skip {fn}")
        except Exception as e:
            logger.error(f"[RTI] {e}")

    @cyber.on_message(
        filters.private & filters.forwarded & filters.user(ADMIN_USERS) & media_filter
    )
    async def forwarded(bot, message):
        origin = message.forward_origin
        if not origin or origin.type != enums.MessageOriginType.CHANNEL:
            return await message.reply("Forward an original message from a channel.")
        chat_id   = origin.chat.id
        last_id   = origin.message_id
        eff_start = await compute_effective_start(chat_id, 2)
        est       = await _est(eff_start, last_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("DB1", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db1"),
             InlineKeyboardButton("DB2", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db2")],
            [InlineKeyboardButton("Both DBs", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both")],
            [InlineKeyboardButton("Cancel", callback_data="cancel_prompt")],
        ])
        await bot.send_message(
            message.from_user.id,
            f"Index Target?\nChannel : `{chat_id}`\nRange   : `{eff_start}` to `{last_id}`\n\n{_mode_badge()}  Est. **{est}**\n\nChoose DB:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("indexlink"))
    async def indexlink(bot, message):
        args = message.text.split()[1:]
        chat_id, start_id, last_id = extract_links(args)
        eff_start = await compute_effective_start(chat_id, start_id)
        est       = await _est(eff_start, last_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("DB1", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db1"),
             InlineKeyboardButton("DB2", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|db2")],
            [InlineKeyboardButton("Both DBs", callback_data=f"idx_db|{chat_id}|{eff_start}|{last_id}|both")],
            [InlineKeyboardButton("Cancel", callback_data="cancel_prompt")],
        ])
        await bot.send_message(
            message.from_user.id,
            f"Index Target?\nChannel : `{chat_id}`\nRange   : `{eff_start}` to `{last_id}`\n\n{_mode_badge()}  Est. **{est}**\n\nChoose DB:",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=kb,
        )

    @cyber.on_callback_query(filters.regex(r"^idx_db\|"))
    async def idx_db_chosen(bot, query):
        await query.answer()
        _, chat_id, s, l, target = query.data.split("|")
        chat_id, s, l = int(chat_id), int(s), int(l)
        est   = await _est(s, l)
        label = {"db1": "DB1", "db2": "DB2", "both": "Both DBs"}.get(target, target)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Proceed", callback_data=f"startidx|{chat_id}|{s}|{l}|{target}")],
            [InlineKeyboardButton("Cancel",  callback_data="cancel_prompt")],
        ])
        try:
            await query.message.edit_text(
                f"Confirm Index\n\nChannel : `{chat_id}`\nRange   : `{s}` to `{l}` ({l-s+1:,} IDs)\nTarget  : **{label}**\n{_mode_badge()}\nEst.    : **{est}**\n\nTap Proceed to start.",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        except Exception:
            pass

    @cyber.on_callback_query(filters.regex(r"^startidx\|"))
    async def start_index(bot, query):
        global index_task
        await query.answer()
        if index_task and not index_task.done():
            await query.answer("Index already running.", show_alert=True)
            return
        try:
            _, chat_id, s, l, target = query.data.split("|")
            chat_id, s, l = int(chat_id), int(s), int(l)
        except Exception as e:
            logger.error(f"[INDEX] startidx parse: {e}")
            return

        label = {"db1": "DB1", "db2": "DB2", "both": "DB1+DB2"}.get(target, target)
        skip_raw = await get_global_setting(INDEX_SKIP_KEY, None)
        try:
            preserved = int(skip_raw) if skip_raw is not None else None
        except Exception:
            preserved = None

        await delete_all_index_states()
        if preserved is not None:
            await set_global_setting(INDEX_SKIP_KEY, preserved)
        await save_bot_state(f"index_{chat_id}", s)

        try:
            await query.message.edit_text(f"Starting... {label}", parse_mode=enums.ParseMode.MARKDOWN)
        except Exception:
            pass

        status_msg = await safe_send(
            bot, query.from_user.id,
            f"Index Starting\nChannel : `{chat_id}`\nRange   : `{s}` to `{l}`\nTarget  : **{label}**\n{_mode_badge()}\n\nProgress updates every {PROGRESS_EVERY}s...",
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        index_task = asyncio.create_task(
            index_files_task(bot, status_msg, chat_id, s, l, target=target)
        )
        logger.info(f"[INDEX] task started  chat={chat_id}  target={target}")

    @cyber.on_callback_query(filters.regex("^cancel_index$"))
    async def cancel(bot, query):
        global index_task
        if index_task and not index_task.done():
            index_task.cancel()
            await query.answer("Cancelling...", show_alert=True)
        else:
            await query.answer("No active index.", show_alert=True)

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
            await query.answer("Already running!", show_alert=True)
            return
        await query.answer("Resuming...")
        try:
            _, chat_id, s, l, target = query.data.split("|")
            chat_id, s, l = int(chat_id), int(s), int(l)
        except Exception as e:
            logger.error(f"resume_yes parse: {e}")
            return
        try:
            await query.message.edit_text(
                f"Resuming...\nChannel `{chat_id}` range `{s}` to `{l}`\n{_mode_badge()}",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=_cancel_kb(),
            )
        except Exception:
            pass
        index_task = asyncio.create_task(
            index_files_task(bot, query.message, chat_id, s, l, target=target)
        )

    @cyber.on_callback_query(filters.regex("^resume_no$"))
    async def resume_no(bot, query):
        await query.answer("Discarding.")
        try:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
            await query.message.edit_text("Discarded. Forward a message to start fresh.", parse_mode=enums.ParseMode.MARKDOWN)
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
        await set_global_setting(INDEX_SKIP_KEY, val)
        await message.reply(f"Skip set - index starts from `{val + 1}`", parse_mode=enums.ParseMode.MARKDOWN)

    @cyber.on_message(filters.private & filters.user(ADMIN_USERS) & filters.command("getskip"))
    async def getskip(bot, message):
        raw = await get_global_setting(INDEX_SKIP_KEY, None)
        await message.reply(f"Skip stored: `{raw!r}`", parse_mode=enums.ParseMode.MARKDOWN)

    return [
        rti, forwarded, indexlink, idx_db_chosen,
        start_index, cancel, cancel_prompt,
        resume_yes, resume_no, setskip, getskip,
    ]
