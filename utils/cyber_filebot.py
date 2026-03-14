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

from pyrogram import Client, enums as pyro_enums
from pyrogram import enums
from pyrogram.errors import FloodWait
from pyrogram import filters as pyro_filters

from config import API_ID, API_HASH, logger, STICKER_ENABLED, PROGRESS_MESSAGE_ENABLED

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
    global _filebot_token, _filebot_channel, _filebot_username

    try:
        from DB.database import get_global_setting
        d = await get_global_setting(DB_KEY) or {}
    except Exception as e:
        logger.warning(f"[FILEBOT] load config error: {e}")
        d = {}

    _filebot_token    = d.get("token")    or None
    _filebot_channel  = d.get("channel")  or None
    _filebot_username = d.get("username") or None

    if _filebot_token and _filebot_channel:
        logger.info(f"[FILEBOT] Config loaded — channel={_filebot_channel} username=@{_filebot_username}")
    else:
        logger.info("[FILEBOT] No file bot configured — using main bot for delivery.")


async def set_filebot_config(token: str, channel_id: int):
    """Admin sets file bot token + deliver channel. Connects immediately and caches username."""
    global _filebot_token, _filebot_channel, _filebot_username

    await _stop_client()

    _filebot_token   = token.strip()
    _filebot_channel = int(channel_id)

    # Connect now to get the username — needed for building start links
    client = await get_filebot_client()
    if client and _filebot_username:
        logger.info(f"[FILEBOT] Connected as @{_filebot_username}")
    else:
        logger.warning("[FILEBOT] Could not connect to get username — link building may fall back to main bot")

    try:
        from DB.database import set_global_setting
        await set_global_setting(DB_KEY, {
            "token":    _filebot_token,
            "channel":  _filebot_channel,
            "username": _filebot_username,   # persist so it loads instantly on restart
        })
    except Exception as e:
        logger.error(f"[FILEBOT] save config error: {e}")

    logger.info(f"[FILEBOT] Config saved — channel={_filebot_channel} username=@{_filebot_username}")


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
    Loaded from DB on startup — no connection needed for link building.
    Falls back to connecting if not cached yet.
    """
    global _filebot_username
    if not is_filebot_configured():
        return None
    if _filebot_username:
        return _filebot_username
    # Not cached yet — connect to fetch it
    client = await get_filebot_client()
    if client and _filebot_username:
        # Persist for future restarts
        try:
            from DB.database import get_global_setting, set_global_setting
            d = await get_global_setting(DB_KEY) or {}
            d["username"] = _filebot_username
            await set_global_setting(DB_KEY, d)
        except Exception:
            pass
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
    global _filebot_client, _filebot_username

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
                               caption: str, source_channel_id: int | None = None,
                               source_message_id: int | None = None) -> int | None:
    """
    Main bot sends a file to the relay channel.
    If the stored file_id is stale, refreshes it from the source channel before retrying.
    Returns the channel message_id, or None on failure.
    """
    from CyberTG.files import _refresh_file_id

    current_file_id = file_id

    for attempt in range(3):
        try:
            if media_type == "audio":
                sent = await main_cyber.send_audio(
                    channel_id, current_file_id,
                    caption=caption, parse_mode=enums.ParseMode.HTML,
                )
            else:
                sent = await main_cyber.send_document(
                    channel_id, current_file_id,
                    caption=caption, parse_mode=enums.ParseMode.HTML,
                )
            return sent.id
        except FloodWait as fw:
            await _fw_sleep(fw)
        except Exception as e:
            err = str(e)
            # Stale file_id — try to refresh once from source channel
            if any(k in err for k in ("MEDIA_EMPTY", "FILE_ID_INVALID", "FILE_REFERENCE_EXPIRED")):
                if source_channel_id and source_message_id and attempt == 0:
                    logger.warning(f"[FILEBOT] stale file_id posting to relay — refreshing from source")
                    fresh = await _refresh_file_id(main_cyber, source_channel_id, source_message_id)
                    if fresh:
                        current_file_id = fresh
                        continue   # retry with fresh file_id
                return None   # cannot refresh — caller will use source fallback
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


async def _delete_channel_msg(main_cyber: Client, channel_id: int, message_id):
    """Clean up relay message(s) from deliver channel after copying.
    Accepts a single message_id (int) or a list of message_ids.
    """
    try:
        ids = message_id if isinstance(message_id, list) else [message_id]
        await main_cyber.delete_messages(channel_id, ids)
    except Exception as e:
        logger.debug(f"[FILEBOT] cleanup channel msg(s): {e}")


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
            # No file_id — fetch from source channel via main bot, send to user via file bot
            try:
                from CyberTG.files import _forward_fallback
                ok = await _forward_fallback(main_cyber, user_id, f, caption, auto_delay,
                                             send_client=file_client)
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

        # ── Load auto-delete delay FIRST so it applies to ALL messages ──
        from utils.cyber_delete import delete_message_later
        from DB.database import get_global_setting as _gset
        try:
            _raw_delay = await _gset("FILE_AUTODEL_DELAY", 0)
            auto_delay = int(_raw_delay) if _raw_delay else 0
        except Exception:
            auto_delay = 0

        # Schedule /start command message for auto-delete (same delay as files)
        # Do NOT delete immediately — let the scheduler handle it cleanly
        if auto_delay > 0:
            delete_message_later(client, uid, message.id, auto_delay)

        # ── FSub check via main bot ───────────────────────────
        try:
            from CyberTG.fsub import (
                get_slot_statuses, all_slots_passed,
                build_fsub_keyboard, fsub_text,
                register_pending_fsub, clear_pending_fsub,
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
                    # via_filebot=True so auto-delivery after join uses file bot path
                    register_pending_fsub(uid, gate_msg, payload, via_filebot=True)
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
            sent = await client.send_message(
                uid,
                "⏳ <b>Link Expired</b>\n"
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                "This download link has expired.\n"
                "<i>Please search for the series again to get a fresh link.</i>",
                parse_mode=pyro_enums.ParseMode.HTML,
            )
            if auto_delay > 0:
                delete_message_later(client, uid, sent.id, auto_delay)
            return

        owner, series, season, lang, quality = decoded

        if int(owner) != uid:
            sent = await client.send_message(
                uid,
                "⛔ <b>Access Denied</b>\n"
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                "<i>This link belongs to another user.</i>",
                parse_mode=pyro_enums.ParseMode.HTML,
            )
            if auto_delay > 0:
                delete_message_later(client, uid, sent.id, auto_delay)
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
            await _deliver_via_filebot_client(main_cyber, client, uid, auto_delay=auto_delay)
        except Exception as e:
            logger.error(f"[FILEBOT] delivery error uid={uid}: {e}")
            sent = await client.send_message(uid, "❌ Delivery failed. Please try again.")
            if auto_delay > 0:
                delete_message_later(client, uid, sent.id, auto_delay)

    logger.info("[FILEBOT] ✅ /start handler registered on file bot client.")


# ─────────────────────────────────────────────────────────────
# FILEBOT-AWARE SEND HELPERS
# Split send_client (file bot) from fallback_client (main bot).
# BUG FIX: _safe_send_single and _safe_send_group from files.py use a
# single `cyber` for both sending AND source-channel fallback.  In the
# file-bot path the file bot cannot access source channels (CHANNEL_INVALID),
# so we must pass main_cyber for _forward_fallback while using file_client
# for the actual DM delivery.
# ─────────────────────────────────────────────────────────────

async def _safe_send_single_filebot(
    send_client, fallback_client, user_id: int, f: dict, caption: str, auto_delay: int
) -> bool:
    """
    Send one file to user via send_client (file bot).

    On stale/invalid file_id the file bot cannot use the main bot's refreshed
    file_id — Telegram file_ids are bot-specific.  In that case we fall back to
    the main bot copying the message directly from the source channel to the user,
    which is always valid as long as the main bot is a channel member.
    """
    from pyrogram.errors import FloodWait, BadRequest
    from pyrogram import enums as _enums
    from utils.cyber_delete import delete_message_later
    from CyberTG.files import _forward_fallback

    file_id    = f.get("file_id")
    media_type = (f.get("media_type") or "document").lower()
    file_name  = f.get("file_name", "unknown")

    if not file_id:
        # No file_id stored — main bot fetches from source channel and sends to user
        return await _forward_fallback(fallback_client, user_id, f, caption, auto_delay,
                                       send_client=fallback_client)

    while True:
        try:
            if media_type == "audio":
                sent = await send_client.send_audio(
                    user_id, file_id,
                    caption=caption, parse_mode=_enums.ParseMode.HTML,
                )
            elif media_type == "video":
                sent = await send_client.send_video(
                    user_id, file_id,
                    caption=caption, parse_mode=_enums.ParseMode.HTML,
                )
            else:
                sent = await send_client.send_document(
                    user_id, file_id,
                    caption=caption, parse_mode=_enums.ParseMode.HTML,
                )
            if sent and auto_delay > 0:
                delete_message_later(send_client, user_id, sent.id, auto_delay)
            return True
        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[FILEBOT] FloodWait {wait}s uid={user_id} — waiting")
            await asyncio.sleep(wait + 1)
        except BadRequest as e:
            err = str(e)
            if any(k in err for k in ("MEDIA_EMPTY", "FILE_ID_INVALID", "FILE_REFERENCE_EXPIRED")):
                logger.warning(f"[FILEBOT] stale file_id for {file_name} — falling back to main bot copy")
                # IMPORTANT: file_ids are bot-specific. We cannot refresh the main bot's
                # file_id and reuse it with the file bot — Telegram will reject it.
                # Instead, have the main bot copy the message directly from the source
                # channel to the user DM. This is reliable and requires no relay.
                return await _forward_fallback(fallback_client, user_id, f, caption, auto_delay,
                                               send_client=fallback_client)
            logger.error(f"[FILEBOT] BadRequest uid={user_id} file={file_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"[FILEBOT] send failed uid={user_id} file={file_name}: {e}")
            return False


async def _safe_send_group_filebot(
    send_client, fallback_client, user_id: int, media_group: list,
    auto_delay: int,
    source_files: list | None = None,
    caption_map: dict | None = None,
) -> list:
    """
    Attempt send_media_group via send_client (file bot).

    On any failure, fall back to per-file delivery via _safe_send_single_filebot.
    Stale file_id handling is delegated entirely to _safe_send_single_filebot,
    which correctly uses the main bot (fallback_client) to copy from the source
    channel — because file_ids are bot-specific and cannot be shared between bots.
    """
    from pyrogram.errors import FloodWait
    from utils.cyber_delete import delete_message_later

    while True:
        try:
            sent_msgs = await send_client.send_media_group(user_id, media_group)
            if auto_delay > 0 and sent_msgs:
                for sm in sent_msgs:
                    delete_message_later(send_client, user_id, sm.id, auto_delay)
            return sent_msgs or []
        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            logger.warning(f"[FILEBOT] FloodWait {wait}s uid={user_id} — waiting")
            await asyncio.sleep(wait + 1)
        except Exception as e:
            logger.error(f"[FILEBOT] send_media_group failed uid={user_id}: {e} — falling back to per-file")
            if source_files:
                sent_fallback = []
                for f in source_files:
                    cap = (caption_map or {}).get(id(f), "") or (f.get("caption") or "").strip()
                    if not cap:
                        ep  = str(f.get("episode", 0)).zfill(2)
                        cap = f"<b>Episode {ep}</b>"
                    ok = await _safe_send_single_filebot(send_client, fallback_client, user_id, f, cap, auto_delay)
                    if ok:
                        sent_fallback.append(True)
                return sent_fallback
            return []


async def _forward_batch_to_channel(
    main_cyber: Client, channel_id: int,
    batch: list,   # list of (f, cap) dicts
) -> list[int]:
    """
    Main bot sends a batch of files to the relay channel as a media group.
    Returns list of channel message_ids (one per file in the batch).
    Falls back to per-file sends if send_media_group fails.
    Handles stale file_ids by refreshing from source channel before retrying.
    """
    from CyberTG.files import _refresh_file_id, _make_input_media
    from pyrogram.types import InputMediaDocument, InputMediaVideo, InputMediaAudio

    # ── Build InputMedia list, refreshing any stale file_ids ──
    async def _resolve(f, cap):
        file_id    = f.get("file_id")
        media_type = (f.get("media_type") or "document").lower()
        if not file_id:
            return None, f, cap
        # Try to use as-is first; we'll refresh on failure
        return file_id, f, cap

    resolved = await asyncio.gather(*[_resolve(f, cap) for f, cap in batch])

    def _make_item(file_id, media_type, cap):
        if media_type == "audio":
            return InputMediaAudio(media=file_id, caption=cap, parse_mode=enums.ParseMode.HTML)
        elif media_type == "video":
            return InputMediaVideo(media=file_id, caption=cap, parse_mode=enums.ParseMode.HTML)
        return InputMediaDocument(media=file_id, caption=cap, parse_mode=enums.ParseMode.HTML)

    # ── Attempt send_media_group ───────────────────────────────
    media_items = []
    valid_files = []
    for file_id, f, cap in resolved:
        if not file_id:
            continue
        media_type = (f.get("media_type") or "document").lower()
        media_items.append(_make_item(file_id, media_type, cap))
        valid_files.append((f, cap))

    if not media_items:
        return []

    for attempt in range(2):
        try:
            sent_msgs = await main_cyber.send_media_group(channel_id, media_items)
            return [m.id for m in sent_msgs]
        except FloodWait as fw:
            await _fw_sleep(fw)
        except Exception as e:
            err = str(e)
            if any(k in err for k in ("MEDIA_EMPTY", "FILE_ID_INVALID", "FILE_REFERENCE_EXPIRED")) and attempt == 0:
                # Refresh all stale file_ids concurrently then rebuild
                logger.warning(f"[FILEBOT] stale file_id in batch — refreshing {len(valid_files)} files")
                fresh_ids = await asyncio.gather(*[
                    _refresh_file_id(main_cyber, f.get("channel_id"), f.get("message_id"))
                    for f, _ in valid_files
                ], return_exceptions=True)
                media_items = []
                refreshed_files = []
                for (f, cap), fresh in zip(valid_files, fresh_ids):
                    fid = fresh if isinstance(fresh, str) and fresh else f.get("file_id")
                    if fid:
                        media_type = (f.get("media_type") or "document").lower()
                        media_items.append(_make_item(fid, media_type, cap))
                        refreshed_files.append((f, cap))
                valid_files = refreshed_files
                continue
            # Group send failed — fall back to per-file
            logger.warning(f"[FILEBOT] send_media_group to channel failed: {e} — per-file fallback")
            break

    # ── Per-file fallback ──────────────────────────────────────
    msg_ids = []
    for f, cap in valid_files:
        file_id    = f.get("file_id")
        media_type = (f.get("media_type") or "document").lower()
        if not file_id:
            continue
        mid = await _forward_to_channel(
            main_cyber, channel_id, file_id, media_type, cap,
            source_channel_id=f.get("channel_id"),
            source_message_id=f.get("message_id"),
        )
        if mid:
            msg_ids.append(mid)
        await asyncio.sleep(0.05)
    return msg_ids


async def _copy_batch_from_channel(
    file_client: Client, user_id: int,
    channel_id: int, message_ids: list[int],
    auto_delay: int,
) -> int:
    """
    File bot copies multiple channel messages to user DM concurrently.
    Returns count of successful copies.
    """
    from utils.cyber_delete import delete_message_later

    sem = asyncio.Semaphore(5)   # max 5 concurrent copies

    async def _copy_one(mid):
        async with sem:
            for attempt in range(3):
                try:
                    sent = await file_client.copy_message(
                        chat_id=user_id,
                        from_chat_id=channel_id,
                        message_id=mid,
                    )
                    if sent and auto_delay > 0:
                        delete_message_later(file_client, user_id, sent.id, auto_delay)
                    return True
                except FloodWait as fw:
                    await _fw_sleep(fw)
                except Exception as e:
                    logger.error(f"[FILEBOT] copy msg {mid} attempt {attempt+1}: {e}")
                    if attempt < 2:
                        await asyncio.sleep(2)
            return False

    results = await asyncio.gather(*[_copy_one(mid) for mid in message_ids],
                                    return_exceptions=True)
    return sum(1 for r in results if r is True)


async def _deliver_via_filebot_client(main_cyber, file_client, user_id: int, on_complete=None, auto_delay: int | None = None):
    """
    Relay delivery with media-group batching:

      For each batch of up to 10 files:
        1. Main bot sends batch → relay channel as send_media_group
        2. File bot copies each msg from channel → user DM (concurrently)
        3. Main bot bulk-deletes the batch from channel

      Files with no file_id or whose relay fails fall back to main-bot
      copy_message from source channel directly to user.
    """
    from DB.database import get_all_files_by_season_and_quality, get_global_setting
    from CyberTG.files import (
        get_session_async, clear_session, clean_series_name,
        _normalize_for_query, _build_caption, DEFAULT_CAPTION,
        _send_start_sticker, _send_end_sticker,
        _forward_fallback, MEDIA_GROUP_SIZE,
    )

    channel_id = _filebot_channel
    if not channel_id:
        logger.warning(f"[FILEBOT] No relay channel configured — cannot deliver uid={user_id}")
        return

    session = await get_session_async(user_id)
    if not session:
        logger.warning(f"[FILEBOT] no session uid={user_id}")
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

    if auto_delay is None:
        auto_delay = await get_global_setting("FILE_AUTODEL_DELAY", 0)
        try:
            auto_delay = int(auto_delay) if auto_delay else 0
        except (TypeError, ValueError):
            auto_delay = 0

    custom_caption   = await get_global_setting("CUSTOM_FILE_CAPTION", None)
    caption_template = custom_caption if custom_caption else DEFAULT_CAPTION

    # Progress message via file bot
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

    # Start sticker via file bot
    await _send_start_sticker(file_client, user_id, season, auto_delay)

    # Split into groupable (have file_id) and solo (no file_id / non-document)
    # Note: send_media_group supports documents, videos, audios in separate groups;
    # we treat all as groupable — _forward_batch_to_channel handles the typing.
    groupable = [(f, _build_caption(caption_template, series, season,
                                    f.get("episode", 0), language, quality,
                                    f.get("file_name", "")))
                 for f in files if f.get("file_id")]
    solo      = [(f, _build_caption(caption_template, series, season,
                                    f.get("episode", 0), language, quality,
                                    f.get("file_name", "")))
                 for f in files if not f.get("file_id")]

    sent_count = 0

    # ── Batch relay via media group ────────────────────────────
    for batch_start in range(0, len(groupable), MEDIA_GROUP_SIZE):
        batch = groupable[batch_start : batch_start + MEDIA_GROUP_SIZE]

        # Step 1: main bot → relay channel (as media group)
        channel_msg_ids = await _forward_batch_to_channel(main_cyber, channel_id, batch)

        if not channel_msg_ids:
            # Entire batch failed — fall back per-file via source channel
            logger.warning(f"[FILEBOT] batch relay failed uid={user_id} — source fallback for {len(batch)} files")
            for f, cap in batch:
                ok = await _forward_fallback(main_cyber, user_id, f, cap, auto_delay)
                if ok:
                    sent_count += 1
            continue

        # Step 2: file bot copies each channel msg → user DM (concurrent)
        copied = await _copy_batch_from_channel(
            file_client, user_id, channel_id, channel_msg_ids, auto_delay
        )
        sent_count += copied

        # Step 3: bulk-delete relay messages from channel (fire-and-forget)
        asyncio.create_task(
            _delete_channel_msg(main_cyber, channel_id, channel_msg_ids)
        )

    # ── Solo files (no file_id) ───────────────────────────────
    for f, cap in solo:
        ok = await _forward_fallback(main_cyber, user_id, f, cap, auto_delay)
        if ok:
            sent_count += 1

    # Cleanup progress message
    if progress_msg:
        try:
            await file_client.delete_messages(user_id, progress_msg.id)
        except Exception:
            pass

    # End sticker via file bot
    await _send_end_sticker(file_client, user_id, season, total, auto_delay)

    if auto_delay > 0:
        from CyberTG.files import _send_autodel_notice
        await _send_autodel_notice(file_client, user_id, auto_delay)

    logger.info(f"[FILEBOT] ✅ Relay delivery done uid={user_id} sent={sent_count}/{total}")

    if on_complete:
        try:
            await on_complete()
        except Exception as e:
            logger.error(f"[FILEBOT] on_complete error uid={user_id}: {e}")
