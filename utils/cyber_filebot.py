"""
cyber_filebot.py — Dual-Bot File Delivery
==========================================

HOW IT WORKS:
  1. Admin sets a "File Bot" via /adminsetting → Global Settings → File Bot
     - File Bot token  (the second bot's token)
     - Deliver Channel (a channel where BOTH bots are admin)

  2. When user requests files:
     - Main bot forwards each file to the Deliver Channel
     - Second bot (file_bot_client) copies those messages from channel → user DM

  3. If no File Bot is configured → main bot delivers directly (existing behavior)

ADMIN SETTINGS (stored in MongoDB global_settings["FILEBOT_CONFIG"]):
  {
    "token":   "<bot_token>",
    "channel": <channel_id (int)>
  }

PUBLIC API:
  await load_filebot_config()          — call on startup (after main bot starts)
  await get_filebot_client()           — returns active Client or None
  await deliver_via_filebot(main_cyber, user_id, files, ...) — send files through file bot
  await set_filebot_config(token, channel_id)  — admin sets config
  await del_filebot_config()                   — admin clears config
  get_filebot_info()                           — returns (token_preview, channel_id) or None
"""

from __future__ import annotations

import asyncio
from typing import Optional

from pyrogram import Client
from pyrogram.errors import FloodWait

from config import API_ID, API_HASH, logger

# ── Module-level state ────────────────────────────────────────
_filebot_token:    str | None = None
_filebot_channel:  int | None = None
_filebot_client:   Client | None = None
_filebot_username: str | None = None   # cached @username of the file bot
_filebot_lock = asyncio.Lock()

DB_KEY = "FILEBOT_CONFIG"


# ═══════════════════════════════════════════════════════════════
# CONFIG LOAD / SAVE
# ═══════════════════════════════════════════════════════════════

async def load_filebot_config():
    """Load file bot config from MongoDB. Call once on startup."""
    global _filebot_token, _filebot_channel

    try:
        from DB.database import get_global_setting
        d = await get_global_setting(DB_KEY) or {}
    except Exception as e:
        logger.warning(f"[FILEBOT] load config error: {e}")
        d = {}

    _filebot_token   = d.get("token")   or None
    _filebot_channel = d.get("channel") or None

    if _filebot_token and _filebot_channel:
        logger.info(f"[FILEBOT] Config loaded — channel={_filebot_channel}")
    else:
        logger.info("[FILEBOT] No file bot configured — using main bot for delivery.")


async def set_filebot_config(token: str, channel_id: int):
    """Admin sets file bot token + deliver channel. Restarts the client."""
    global _filebot_token, _filebot_channel

    _filebot_token   = token.strip()
    _filebot_channel = int(channel_id)

    try:
        from DB.database import set_global_setting
        await set_global_setting(DB_KEY, {"token": _filebot_token, "channel": _filebot_channel})
    except Exception as e:
        logger.error(f"[FILEBOT] save config error: {e}")

    # Stop old client if running
    await _stop_client()
    logger.info(f"[FILEBOT] Config saved — channel={_filebot_channel}")


async def del_filebot_config():
    """Admin removes file bot config. Falls back to main bot delivery."""
    global _filebot_token, _filebot_channel

    await _stop_client()
    _filebot_token   = None
    _filebot_channel = None

    try:
        from DB.database import set_global_setting
        await set_global_setting(DB_KEY, {})
    except Exception as e:
        logger.error(f"[FILEBOT] del config error: {e}")

    logger.info("[FILEBOT] Config cleared — delivery reverts to main bot.")


def get_filebot_info() -> tuple[str, int] | None:
    """Returns (token_preview, channel_id) or None if not configured."""
    if not _filebot_token or not _filebot_channel:
        return None
    preview = _filebot_token[:8] + "..." + _filebot_token[-6:]
    return preview, _filebot_channel


def is_filebot_configured() -> bool:
    return bool(_filebot_token and _filebot_channel)


async def get_filebot_username() -> str | None:
    """
    Returns the @username of the file bot (without @).
    Connects the client if not already connected.
    Returns None if no file bot configured.
    """
    global _filebot_username
    if not is_filebot_configured():
        return None
    if _filebot_username:
        return _filebot_username
    # Connect to get username
    client = await get_filebot_client()
    if client:
        return _filebot_username
    return None


# ═══════════════════════════════════════════════════════════════
# CLIENT MANAGEMENT
# ═══════════════════════════════════════════════════════════════

async def _stop_client():
    global _filebot_client, _filebot_username
    if _filebot_client:
        try:
            if _filebot_client.is_connected:
                await _filebot_client.stop()
        except Exception as e:
            logger.debug(f"[FILEBOT] stop client: {e}")
        _filebot_client = None
        _filebot_username = None


async def get_filebot_client() -> Client | None:
    """
    Returns a connected file bot Client, or None if not configured.
    Lazily starts the client on first call.
    """
    global _filebot_client

    if not _filebot_token or not _filebot_channel:
        return None

    async with _filebot_lock:
        # Recheck after lock
        if _filebot_client and _filebot_client.is_connected:
            return _filebot_client

        # Create and start a new client
        try:
            client = Client(
                name="FileSenderBot",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=_filebot_token,
                in_memory=True,          # no session file needed
            )
            await client.start()
            _filebot_client = client
            me = await client.get_me()
            _filebot_username = me.username
            logger.info(f"[FILEBOT] ✅ File bot connected: @{me.username}")
            return _filebot_client
        except Exception as e:
            logger.error(f"[FILEBOT] ❌ Could not start file bot client: {e}")
            _filebot_client = None
            return None


# ═══════════════════════════════════════════════════════════════
# RELAY DELIVERY
# ═══════════════════════════════════════════════════════════════

async def _fw_sleep(fw: FloodWait):
    wait = max(int(getattr(fw, "value", 10)), 5)
    logger.warning(f"[FILEBOT] FloodWait {wait}s — sleeping")
    await asyncio.sleep(wait + 2)


async def _forward_to_channel(main_cyber: Client, channel_id: int,
                               file_id: str, media_type: str,
                               caption: str) -> int | None:
    """
    Main bot sends a file to the deliver channel.
    Returns the message_id in the channel, or None on failure.
    """
    from pyrogram import enums
    for attempt in range(3):
        try:
            if media_type == "audio":
                sent = await main_cyber.send_audio(
                    channel_id, file_id,
                    caption=caption, parse_mode=enums.ParseMode.HTML,
                )
            else:
                sent = await main_cyber.send_document(
                    channel_id, file_id,
                    caption=caption, parse_mode=enums.ParseMode.HTML,
                )
            return sent.id
        except FloodWait as fw:
            await _fw_sleep(fw)
        except Exception as e:
            logger.error(f"[FILEBOT] forward to channel attempt {attempt+1}: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return None


async def _copy_from_channel(file_client: Client, user_id: int,
                              channel_id: int, message_id: int,
                              auto_delay: int) -> bool:
    """
    File bot copies a message from deliver channel → user DM.
    """
    from utils.cyber_delete import delete_message_later
    for attempt in range(3):
        try:
            sent = await file_client.copy_message(
                chat_id=user_id,
                from_chat_id=channel_id,
                message_id=message_id,
            )
            if sent and auto_delay > 0:
                delete_message_later(file_client, user_id, sent.id, auto_delay)
            return True
        except FloodWait as fw:
            await _fw_sleep(fw)
        except Exception as e:
            logger.error(f"[FILEBOT] copy to user attempt {attempt+1}: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return False


async def _delete_channel_msg(main_cyber: Client, channel_id: int, message_id: int):
    """Clean up the relay message from deliver channel after copying."""
    try:
        await main_cyber.delete_messages(channel_id, message_id)
    except Exception as e:
        logger.debug(f"[FILEBOT] cleanup channel msg {message_id}: {e}")


async def deliver_via_filebot(
    main_cyber: Client,
    user_id: int,
    files: list,
    caption_builder,          # callable(f) → str
    auto_delay: int = 0,
    on_complete=None,
) -> bool:
    """
    Dual-bot delivery:
      For each file:
        1. Main bot sends file → deliver channel
        2. File bot copies channel msg → user DM
        3. Main bot deletes channel msg (keep channel clean)

    Falls back to main-bot direct delivery if file bot is unavailable.

    Returns True if delivery completed via file bot, False if fell back.
    """
    file_client = await get_filebot_client()
    channel_id  = _filebot_channel

    if not file_client or not channel_id:
        logger.info(f"[FILEBOT] No file bot available — falling back to direct delivery uid={user_id}")
        return False  # caller handles fallback

    logger.info(f"[FILEBOT] Starting relay delivery uid={user_id} files={len(files)} channel={channel_id}")

    sent_count = 0

    for f in files:
        file_id    = f.get("file_id")
        media_type = (f.get("media_type") or "document").lower()
        caption    = caption_builder(f)

        if not file_id:
            # No file_id — use fallback forward directly to user via main bot
            try:
                from CyberTG.files import _forward_fallback
                ok = await _forward_fallback(main_cyber, user_id, f, caption, auto_delay)
                if ok:
                    sent_count += 1
            except Exception as e:
                logger.error(f"[FILEBOT] forward_fallback error: {e}")
            continue

        # Step 1: Main bot → deliver channel
        channel_msg_id = await _forward_to_channel(
            main_cyber, channel_id, file_id, media_type, caption
        )

        if not channel_msg_id:
            logger.warning(f"[FILEBOT] Failed to post to channel for uid={user_id} — skipping file")
            continue

        # Step 2: File bot → user DM
        ok = await _copy_from_channel(file_client, user_id, channel_id, channel_msg_id, auto_delay)
        if ok:
            sent_count += 1

        # Step 3: Cleanup channel msg (fire-and-forget)
        asyncio.create_task(_delete_channel_msg(main_cyber, channel_id, channel_msg_id))

        # Small yield to avoid blocking event loop
        await asyncio.sleep(0.05)

    logger.info(f"[FILEBOT] ✅ Relay delivery done uid={user_id} sent={sent_count}/{len(files)}")

    if on_complete:
        try:
            await on_complete()
        except Exception as e:
            logger.error(f"[FILEBOT] on_complete error: {e}")

    return True


# ═══════════════════════════════════════════════════════════════
# FILE BOT /start HANDLER
# ═══════════════════════════════════════════════════════════════

def register_filebot_start_handler(main_cyber):
    """
    Registers a /start handler on the file bot client that:
      1. Decodes the send_ payload
      2. Checks FSub (using main bot for member checks)
      3. Sends start sticker + files + end sticker via the FILE BOT
      4. Auto-deletes if configured

    Call this once after load_filebot_config() on startup,
    and again after set_filebot_config() when admin updates config.

    The handler is re-registered each time the client restarts.
    We store it as a module-level flag so it isn't double-registered.
    """
    # Store reference so we can re-register after client restart
    _m = __import__(__name__, fromlist=["_pending_start_handler"])
    _m._main_cyber_ref = main_cyber
    _m._start_handler_registered = False
    asyncio.create_task(_ensure_start_handler(main_cyber))


async def _ensure_start_handler(main_cyber):
    """Connect client and attach the /start handler."""
    global _filebot_username

    client = await get_filebot_client()
    if not client:
        logger.warning("[FILEBOT] Cannot register start handler — client unavailable.")
        return

    # Pyrogram handlers are registered per-client instance.
    # We use a closure so each new client gets its own handler.
    from pyrogram import filters as pyro_filters
    from pyrogram import enums as pyro_enums

    @client.on_message(pyro_filters.command("start") & pyro_filters.private)
    async def _filebot_start(_, message):
        uid = message.from_user.id

        # Add user to DB (same as main bot /start)
        try:
            from DB.database import add_user
            await add_user(uid)
        except Exception:
            pass

        if len(message.command) < 2 or not message.command[1].startswith("send_"):
            # No payload — just greet
            try:
                bot = await client.get_me()
                name = bot.first_name
                await message.reply_text(
                    f"👋 <b>Hi!</b> I am <b>{name}</b>, the file delivery bot.\n\n"
                    "Use the main bot to search and request files.",
                    parse_mode=pyro_enums.ParseMode.HTML,
                )
            except Exception:
                pass
            return

        payload = message.command[1]

        # Delete the /start message to keep DM clean
        try:
            await message.delete()
        except Exception:
            pass

        # ── FSub check via main bot ───────────────────────────
        try:
            from CyberTG.fsub import (
                get_slot_statuses, all_slots_passed,
                build_fsub_keyboard, fsub_text,
                register_pending_fsub, clear_pending_fsub,
                do_deliver_via_filebot,
                _PENDING_FSUB,
            )
            statuses = await get_slot_statuses(main_cyber, uid)
            passed   = all_slots_passed(statuses)

            if not passed:
                main_bot = await main_cyber.get_me()
                # Build keyboard pointing to MAIN bot for fsub (join channel links)
                kb  = build_fsub_keyboard(main_bot.username, payload, statuses)
                txt = fsub_text(statuses)

                existing = _PENDING_FSUB.get(uid)
                if existing and existing["payload"] == payload:
                    try:
                        await existing["msg"].edit_text(
                            txt, reply_markup=kb,
                            parse_mode=pyro_enums.ParseMode.HTML,
                        )
                    except Exception:
                        pass
                else:
                    gate_msg = await client.send_message(
                        uid, txt, reply_markup=kb,
                        parse_mode=pyro_enums.ParseMode.HTML,
                    )
                    register_pending_fsub(uid, gate_msg, payload)
                return

            # Clear any pending fsub gate
            pending = _PENDING_FSUB.get(uid)
            if pending:
                try:
                    await pending["msg"].delete()
                except Exception:
                    pass
                clear_pending_fsub(uid)

        except Exception as e:
            logger.warning(f"[FILEBOT] FSub check error uid={uid}: {e}")

        # ── Decode payload ────────────────────────────────────
        try:
            from utils.cyber_filter import decode_payload_async
            decoded = await decode_payload_async(payload)
        except Exception as e:
            logger.error(f"[FILEBOT] decode_payload error: {e}")
            decoded = None

        if not decoded:
            await client.send_message(
                uid,
                "⏳ <b>Link Expired</b>\n"
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                "This download link has expired.\n"
                "<i>Please search for the series again to get a fresh link.</i>",
                parse_mode=pyro_enums.ParseMode.HTML,
            )
            return

        owner, series, season, lang, quality = decoded

        if int(owner) != uid:
            await client.send_message(
                uid,
                "⛔ <b>Access Denied</b>\n"
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                "<i>This link belongs to another user.</i>",
                parse_mode=pyro_enums.ParseMode.HTML,
            )
            return

        # ── Set session + deliver via FILE BOT directly ───────
        try:
            from CyberTG.files import set_session
            set_session(uid, {
                "series":   series,
                "season":   int(season),
                "language": lang,
                "quality":  quality,
            })
            await _deliver_via_filebot_client(main_cyber, client, uid)
        except Exception as e:
            logger.error(f"[FILEBOT] delivery error uid={uid}: {e}")
            await client.send_message(uid, "❌ Delivery failed. Please try again.")

    logger.info("[FILEBOT] ✅ /start handler registered on file bot client.")


async def _deliver_via_filebot_client(main_cyber, file_client, user_id: int):
    """
    Full delivery via the file bot client directly.
    Sends sticker + files + end sticker all through file_client (second bot).
    Files are fetched from DB and sent directly using file_client — NO relay channel needed.
    """
    import re
    from config import logger, STICKER_ENABLED, PROGRESS_MESSAGE_ENABLED
    from DB.database import get_all_files_by_season_and_quality, get_global_setting
    from CyberTG.files import (
        get_session_async, clear_session, clean_series_name,
        _normalize_for_query, _build_caption, DEFAULT_CAPTION,
        _safe_send_single, _safe_send_group, _make_input_media,
        _send_start_sticker, _send_end_sticker,
        MEDIA_GROUP_SIZE,
    )
    from pyrogram import enums as pyro_enums

    session = await get_session_async(user_id)
    if not session:
        logger.warning(f"[FILEBOT] _deliver_via_filebot_client: no session uid={user_id}")
        return

    raw_series = session["series"]
    series     = clean_series_name(raw_series)
    season     = int(session["season"])
    language   = session["language"]
    quality    = session["quality"]
    query_name = _normalize_for_query(raw_series)

    files = await get_all_files_by_season_and_quality(query_name, season, quality, language)

    if not files:
        await file_client.send_message(user_id, "❌ No files found.")
        clear_session(user_id)
        return

    total = len(files)
    clear_session(user_id)

    auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
    try:
        auto_delay = int(auto_delay) if auto_delay else 0
    except (TypeError, ValueError):
        auto_delay = 0

    custom_caption   = await get_global_setting("CUSTOM_FILE_CAPTION", None)
    caption_template = custom_caption if custom_caption else DEFAULT_CAPTION

    # Progress message (via file bot)
    progress_msg = None
    if PROGRESS_MESSAGE_ENABLED:
        try:
            progress_msg = await file_client.send_message(
                user_id,
                f"📦 <b>Preparing {total} files...</b>",
                parse_mode=pyro_enums.ParseMode.HTML,
            )
        except Exception:
            pass

    # Start sticker (via file bot)
    await _send_start_sticker(file_client, user_id, season, auto_delay)

    # Build all items
    all_items = []
    for f in files:
        cap = _build_caption(
            caption_template, series, season,
            f.get("episode", 0), language, quality,
            f.get("file_name", ""),
        )
        raw_type  = (f.get("media_type") or "document").lower()
        norm_type = "document" if raw_type in ("video", "document") else raw_type
        all_items.append((f, cap, f.get("file_id"), norm_type))

    groupable = [(f, cap, fid) for f, cap, fid, t in all_items if t == "document" and fid]
    solo      = [(f, cap)      for f, cap, fid, t in all_items if t != "document" or not fid]

    sent_count = 0

    # Send groupable in batches of up to 10 via FILE BOT
    for batch_start in range(0, len(groupable), MEDIA_GROUP_SIZE):
        batch = groupable[batch_start : batch_start + MEDIA_GROUP_SIZE]
        if len(batch) == 1:
            f, cap, fid = batch[0]
            ok = await _safe_send_single(file_client, user_id, f, cap, auto_delay)
            if ok:
                sent_count += 1
        else:
            media_list   = [_make_input_media(f, cap) for f, cap, fid in batch]
            source_files = [f for f, cap, fid in batch]
            caption_map  = {id(f): cap for f, cap, fid in batch}
            sent = await _safe_send_group(
                file_client, user_id, media_list, auto_delay,
                source_files=source_files,
                caption_map=caption_map,
            )
            sent_count += len(sent)

    # Solo files
    for f, cap in solo:
        ok = await _safe_send_single(file_client, user_id, f, cap, auto_delay)
        if ok:
            sent_count += 1

    # Cleanup
    if progress_msg:
        try:
            await file_client.delete_messages(user_id, progress_msg.id)
        except Exception:
            pass

    # End sticker (via file bot)
    await _send_end_sticker(file_client, user_id, season, total, auto_delay)

    logger.info(f"[FILEBOT] ✅ Direct delivery done uid={user_id} sent={sent_count}/{total}")
