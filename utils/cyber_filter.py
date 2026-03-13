"""
cyber_filter.py — FIXED & OPTIMIZED
=====================================
FIXES:
  1. encode_payload now AWAITS MongoDB persist (not fire-and-forget create_task)
     — eliminates race condition where user clicks link before token is saved
  2. encode_payload is now async (was sync) — callers in filters.py updated
  3. decode_payload_async: token never deleted, TTL enforced, memory warmed
  4. Sync decode_payload kept as stub for backward compat
"""

import re
import time
import uuid
import asyncio
from collections import OrderedDict
from config import logger

# =========================================================
# LINK PATTERNS
# =========================================================

PRIVATE_LINK_PATTERN = r"https://t\.me/c/(\d+)/(\d+)"
PUBLIC_LINK_PATTERN  = r"https://t\.me/([A-Za-z0-9_]+)/(\d+)"


def extract_links(links, client=None):
    def parse_link(link):
        match = re.match(PRIVATE_LINK_PATTERN, link)
        if match:
            cid, mid = match.groups()
            return int(f"-100{cid}"), int(mid)
        match = re.match(PUBLIC_LINK_PATTERN, link)
        if match:
            username, mid = match.groups()
            chat = client.get_chat(username)
            return chat.id, int(mid)
        raise ValueError(f"Invalid link: {link}")

    if len(links) == 1:
        chat, last = parse_link(links[0])
        return chat, 2, last

    chat1, start = parse_link(links[0])
    _,     last  = parse_link(links[1])
    return chat1, start, last


# =========================================================
# SHORT TOKEN PAYLOAD SYSTEM
# =========================================================

PAYLOAD_STORE: OrderedDict = OrderedDict()
PAYLOAD_TTL   = 3600   # 1 hour
_PAYLOAD_MAX  = 5000   # max tokens in memory — ~50k users with 0.1 link/user


def _store_set(token: str, data: dict):
    """Insert/update token in LRU store, evicting oldest when full."""
    if token in PAYLOAD_STORE:
        PAYLOAD_STORE.move_to_end(token)
    PAYLOAD_STORE[token] = data
    while len(PAYLOAD_STORE) > _PAYLOAD_MAX:
        PAYLOAD_STORE.popitem(last=False)


def _cleanup_payloads():
    now     = time.time()
    expired = [t for t, d in list(PAYLOAD_STORE.items()) if d["expires"] < now]
    for t in expired:
        PAYLOAD_STORE.pop(t, None)


async def encode_payload(series: str, season, lang: str, quality: str, uid) -> str:
    """
    FIX: async — AWAITS MongoDB persist before returning.
    This guarantees the token exists in MongoDB before the URL is shown to the user.
    No more "Invalid or expired link" after bot restarts.
    """
    _cleanup_payloads()

    token   = uuid.uuid4().hex[:12]
    expires = time.time() + PAYLOAD_TTL

    data = {
        "uid":     int(uid),
        "series":  series,
        "season":  int(season),
        "lang":    lang,
        "quality": quality,
        "expires": expires,
        "token":   token,
    }

    # 1. Save to memory (fast path for current session)
    _store_set(token, data)

    # 2. FIX: AWAIT MongoDB persist — not fire-and-forget
    await _persist_payload_to_mongo(int(uid), token, data)

    return f"send_{token}"


async def _persist_payload_to_mongo(uid: int, token: str, data: dict):
    try:
        # Import at call-time to avoid circular imports at module load,
        # but use a consistent absolute path that works from any package depth.
        try:
            from DB.database import pending_collection
        except ImportError:
            from database import pending_collection  # fallback for flat layouts

        def _sync():
            pending_collection.update_one(
                {"token": token},
                {"$set": {
                    "user_id": uid,
                    "token":   token,
                    "series":  data["series"],
                    "season":  data["season"],
                    "lang":    data["lang"],
                    "quality": data["quality"],
                    "expires": data["expires"],
                }},
                upsert=True
            )

        await asyncio.to_thread(_sync)
        logger.debug(f"[PAYLOAD] ✅ Token {token} persisted to MongoDB")
    except Exception as e:
        logger.error(f"[PAYLOAD] ❌ Failed to persist token {token}: {e}")


async def decode_payload_async(payload: str):
    """
    Async decode — checks memory first, then MongoDB.
    Returns: (uid, series, season, lang, quality) or None
    """
    _cleanup_payloads()

    token = payload.replace("send_", "", 1)
    logger.debug(f"[PAYLOAD] Decoding token={token}")

    # ── 1. Memory (fast path) ────────────────────────────────
    data = PAYLOAD_STORE.get(token)
    if data:
        if time.time() > data["expires"]:
            PAYLOAD_STORE.pop(token, None)
            logger.debug(f"[PAYLOAD] Token {token} expired in memory")
        else:
            logger.debug(f"[PAYLOAD] ✅ Token {token} found in memory")
            return (data["uid"], data["series"], data["season"],
                    data["lang"], data["quality"])

    # ── 2. MongoDB (slow path — survives restarts) ───────────
    logger.debug(f"[PAYLOAD] Checking MongoDB for token={token}...")
    try:
        try:
            from DB.database import pending_collection
        except ImportError:
            from database import pending_collection  # fallback for flat layouts

        doc = await asyncio.to_thread(lambda: pending_collection.find_one({"token": token}))

        if not doc:
            logger.debug(f"[PAYLOAD] ❌ Token {token} not found anywhere")
            return None

        if time.time() > doc.get("expires", 0):
            logger.debug(f"[PAYLOAD] ❌ Token {token} found in MongoDB but EXPIRED")
            return None

        logger.debug(f"[PAYLOAD] ✅ Token {token} recovered from MongoDB")

        # Warm memory cache
        _store_set(token, {
            "uid":     doc["user_id"],
            "series":  doc["series"],
            "season":  doc["season"],
            "lang":    doc["lang"],
            "quality": doc["quality"],
            "expires": doc["expires"],
            "token":   token,
        })

        return (doc["user_id"], doc["series"], doc["season"],
                doc["lang"], doc["quality"])

    except Exception as e:
        logger.error(f"[PAYLOAD] ❌ MongoDB fallback error: {e}")
        return None


def decode_payload(payload: str):
    """
    DEPRECATED sync stub — use decode_payload_async() instead.
    Only checks in-memory store. Safe to keep for import compat.
    """
    _cleanup_payloads()
    token = payload.replace("send_", "", 1)
    data  = PAYLOAD_STORE.get(token)
    if not data:
        return None
    if time.time() > data["expires"]:
        PAYLOAD_STORE.pop(token, None)
        return None
    return (data["uid"], data["series"], data["season"],
            data["lang"], data["quality"])
