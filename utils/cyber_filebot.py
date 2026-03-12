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
_filebot_token:   str | None = None
_filebot_channel: int | None = None
_filebot_client:  Client | None = None
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


# ═══════════════════════════════════════════════════════════════
# CLIENT MANAGEMENT
# ═══════════════════════════════════════════════════════════════

async def _stop_client():
    global _filebot_client
    if _filebot_client:
        try:
            if _filebot_client.is_connected:
                await _filebot_client.stop()
        except Exception as e:
            logger.debug(f"[FILEBOT] stop client: {e}")
        _filebot_client = None


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
