"""
utils/session.py  —  INDEX SESSION STRING MANAGER
===================================================

HOW IT WORKS
────────────
User accounts can call messages.GetHistory (Telegram's fast bulk history API).
Bots cannot — they must use messages.GetMessages (one round-trip per ID batch).

Using a user session string gives:
  • ~10-50× faster indexing  (GetHistory: 100 msgs per call vs bot's ~200 IDs/call
    with mandatory delays to avoid FloodWait)
  • Virtually zero FloodWait  (GetHistory has a much higher Telegram rate limit)
  • No empty ID gaps          (history is sequential, nothing to check)

LIFECYCLE (per index run)
─────────────────────────
  1.  Admin adds session string via admin panel → validated + encrypted → saved in MongoDB
  2.  Admin starts bulk index
  3.  get_index_client() is called → decrypts string → starts Pyrogram user client → returns it
  4.  Indexing runs using user client (GetHistory)
  5.  terminate_index_client() called in finally block → client DISCONNECTED immediately
  6.  Next time admin starts index → same stored string is reused (step 3 again)
  7.  Admin can remove the string at any time from the admin panel

SECURITY
────────
  • Session string is XOR-encrypted with a key derived from the bot token before
    being stored in MongoDB.  Plain string is never written to disk or logged.
  • Client is never kept alive between index runs.
  • If session is revoked/expired, it is auto-deleted from MongoDB and the bot
    falls back to bot-mode indexing automatically.

HOW TO GET A SESSION STRING  (run once locally, never on the server)
─────────────────────────────────────────────────────────────────────
  pip install pyrogram tgcrypto
  python3 - <<'EOF'
  import asyncio
  from pyrogram import Client
  async def main():
      async with Client("tmp", api_id=YOUR_API_ID, api_hash="YOUR_API_HASH") as app:
          print(await app.export_session_string())
  asyncio.run(main())
  EOF
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Optional

from pyrogram import Client
from pyrogram.errors import (
    AuthKeyUnregistered,
    UserDeactivated,
    SessionRevoked,
    FloodWait,
)

from config import API_ID, API_HASH, BOT_TOKEN, logger
from DB.database import get_global_setting, set_global_setting

# ─── MongoDB key ──────────────────────────────────────────────
_DB_KEY = "INDEX_SESSION_STRING"

# ─── Runtime state ───────────────────────────────────────────
_active_client: Optional[Client] = None


# ═════════════════════════════════════════════════════════════
# ENCRYPTION  (XOR + base64  —  keeps string unreadable in Mongo)
# ═════════════════════════════════════════════════════════════

def _key() -> bytes:
    return hashlib.sha256(BOT_TOKEN.encode()).digest()


def _encrypt(text: str) -> str:
    raw = text.encode()
    k   = _key()
    return base64.urlsafe_b64encode(
        bytes(b ^ k[i % len(k)] for i, b in enumerate(raw))
    ).decode()


def _decrypt(blob: str) -> str:
    raw = base64.urlsafe_b64decode(blob.encode())
    k   = _key()
    return bytes(b ^ k[i % len(k)] for i, b in enumerate(raw)).decode()


# ═════════════════════════════════════════════════════════════
# STORAGE API
# ═════════════════════════════════════════════════════════════

async def save_session_string(session_str: str) -> bool:
    """Encrypt and persist session string to MongoDB."""
    try:
        await set_global_setting(_DB_KEY, _encrypt(session_str.strip()))
        logger.info("[SESSION] Session string saved (encrypted).")
        return True
    except Exception as e:
        logger.error(f"[SESSION] save failed: {e}")
        return False


async def load_session_string() -> Optional[str]:
    """Decrypt and return session string, or None if not stored."""
    try:
        blob = await get_global_setting(_DB_KEY, None)
        return _decrypt(blob) if blob else None
    except Exception as e:
        logger.error(f"[SESSION] load failed: {e}")
        return None


async def remove_session_string() -> bool:
    """Delete stored session string from MongoDB."""
    try:
        await set_global_setting(_DB_KEY, None)
        logger.info("[SESSION] Session string removed.")
        return True
    except Exception as e:
        logger.error(f"[SESSION] remove failed: {e}")
        return False


async def has_session_string() -> bool:
    """True if a session string is currently stored."""
    return bool(await get_global_setting(_DB_KEY, None))


# ═════════════════════════════════════════════════════════════
# VALIDATION  (used by admin UI before saving)
# ═════════════════════════════════════════════════════════════

async def validate_session_string(session_str: str) -> tuple[bool, str]:
    """
    Spin up a temporary client with the given string, verify it works,
    then disconnect immediately.

    Returns:
      (True,  "First Last (@username)")  — valid session
      (False, "error description")       — invalid / expired / flood
    """
    client: Optional[Client] = None
    try:
        client = Client(
            name="validate_tmp",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_str.strip(),
            no_updates=True,
            in_memory=True,
        )
        await client.start()
        me   = await client.get_me()
        name = f"{me.first_name or ''} (@{me.username or me.id})".strip()
        return True, name
    except (AuthKeyUnregistered, SessionRevoked, UserDeactivated):
        return False, "Session revoked or account deactivated."
    except FloodWait as fw:
        return False, f"FloodWait {fw.value}s — try again shortly."
    except Exception as e:
        return False, str(e)
    finally:
        if client:
            try:
                await client.stop()
            except Exception:
                pass


# ═════════════════════════════════════════════════════════════
# CLIENT LIFECYCLE  (called by index.py)
# ═════════════════════════════════════════════════════════════

async def get_index_client() -> tuple[Optional[Client], str]:
    """
    Called at the START of every bulk index run.

    Returns (user_client, "user") if a valid session string is stored.
    Returns (None, "bot")         if no session or session is invalid.

    The returned client is ALREADY STARTED.
    Caller MUST call terminate_index_client() in the finally block.
    """
    global _active_client

    session_str = await load_session_string()
    if not session_str:
        logger.info("[SESSION] No session stored — using bot mode.")
        return None, "bot"

    client: Optional[Client] = None
    try:
        client = Client(
            name="index_user",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_str,
            no_updates=True,    # never receive updates from user client
            in_memory=True,     # never write .session file to disk
        )
        await client.start()
        me = await client.get_me()
        logger.info(f"[SESSION] ✅ User client ready: {me.first_name} (id={me.id})")
        _active_client = client
        return client, "user"

    except (AuthKeyUnregistered, SessionRevoked, UserDeactivated) as e:
        logger.warning(f"[SESSION] Session invalid ({type(e).__name__}) — removing, falling back to bot.")
        await remove_session_string()
        if client:
            try: await client.stop()
            except Exception: pass
        return None, "bot"

    except Exception as e:
        logger.error(f"[SESSION] Client start failed: {e} — falling back to bot.")
        if client:
            try: await client.stop()
            except Exception: pass
        return None, "bot"


async def terminate_index_client():
    """
    Called in the finally block of every index run.
    Disconnects and clears the user client.
    Safe to call even if no user client is active (no-op).
    """
    global _active_client
    if _active_client is None:
        return
    try:
        await _active_client.stop()
        logger.info("[SESSION] ✅ User client terminated after index.")
    except Exception as e:
        logger.warning(f"[SESSION] Client stop warning (non-fatal): {e}")
    finally:
        _active_client = None


def is_user_client_active() -> bool:
    """True if a user client is currently live (during an index run)."""
    return _active_client is not None
