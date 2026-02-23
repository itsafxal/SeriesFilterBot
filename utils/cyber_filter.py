import re
import time
import uuid
import asyncio

# =========================================================
# LINK PATTERNS
# =========================================================

PRIVATE_LINK_PATTERN = r"https://t\.me/c/(\d+)/(\d+)"
PUBLIC_LINK_PATTERN  = r"https://t\.me/([A-Za-z0-9_]+)/(\d+)"
JOINCHAT_PATTERN     = r"https://t\.me/\+([A-Za-z0-9_-]+)"


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
        raise ValueError("Invalid link")

    if len(links) == 1:
        chat, last = parse_link(links[0])
        return chat, 2, last

    chat1, start = parse_link(links[0])
    chat2, last  = parse_link(links[1])
    return chat1, start, last


# =========================================================
# SHORT TOKEN PAYLOAD SYSTEM
# =========================================================
#
# ROOT CAUSE OF "ACCESS RESTRICTED" BUG:
#   PAYLOAD_STORE is in-memory only.
#   Flow that breaks everything:
#     1. User clicks download → token created in memory
#     2. FSub check fails → "Access Restricted" shown
#     3. Bot restarts (Render/Railway free tier, crash, redeploy)
#     4. User joins channel → clicks RETRY → same token URL
#     5. decode_payload() → token NOT in memory → returns None
#     6. "Invalid or expired link." → user stuck forever
#
# FIX:
#   encode_payload → saves to memory AND MongoDB
#   decode_payload_async → checks memory first, falls back to MongoDB
#   Tokens survive restarts. TTL still enforced.
# =========================================================

PAYLOAD_STORE: dict = {}
PAYLOAD_TTL   = 3600  # 1 hour — gives user plenty of time to join & retry


def _cleanup_payloads():
    """Remove expired in-memory payloads."""
    now     = time.time()
    expired = [t for t, d in list(PAYLOAD_STORE.items()) if d["expires"] < now]
    for t in expired:
        PAYLOAD_STORE.pop(t, None)


def encode_payload(series: str, season, lang: str, quality: str, uid) -> str:
    """
    Create a short token and persist it to BOTH memory and MongoDB.
    Returns the ?start= payload string.
    """
    _cleanup_payloads()

    token = uuid.uuid4().hex[:12]
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

    # 1. Always save to memory
    PAYLOAD_STORE[token] = data

    # 2. Persist to MongoDB asynchronously (survives restarts)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_persist_payload_to_mongo(int(uid), token, data))
    except Exception:
        pass  # Non-fatal — memory copy handles current session

    return f"send_{token}"


async def _persist_payload_to_mongo(uid: int, token: str, data: dict):
    """Write payload to MongoDB pending_delivery collection."""
    try:
        from DB.database import mongo_client
        col = mongo_client["SeriesBotDB"]["pending_delivery"]

        def _sync():
            col.update_one(
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
        print(f"[PAYLOAD] ✅ Token {token} persisted to MongoDB")
    except Exception as e:
        print(f"[PAYLOAD] ❌ Failed to persist token {token} to MongoDB: {e}")


async def decode_payload_async(payload: str):
    """
    Async decode — checks memory first, then MongoDB.

    Use this everywhere instead of the old sync decode_payload().
    Token is NEVER deleted on decode — TTL handles cleanup.

    Returns: (uid, series, season, lang, quality)  or  None
    """
    _cleanup_payloads()

    token = payload.replace("send_", "", 1)
    print(f"[PAYLOAD] Decoding token={token}")

    # ── 1. Memory (fast path) ────────────────────────────────
    data = PAYLOAD_STORE.get(token)
    if data:
        if time.time() > data["expires"]:
            print(f"[PAYLOAD] Token {token} expired in memory")
            PAYLOAD_STORE.pop(token, None)
        else:
            print(f"[PAYLOAD] ✅ Token {token} found in memory")
            return (data["uid"], data["series"], data["season"],
                    data["lang"], data["quality"])

    # ── 2. MongoDB (slow path — survives restarts) ───────────
    print(f"[PAYLOAD] Token {token} not in memory — checking MongoDB...")
    try:
        from DB.database import mongo_client

        col = mongo_client["SeriesBotDB"]["pending_delivery"]

        def _sync():
            return col.find_one({"token": token})

        doc = await asyncio.to_thread(_sync)

        if not doc:
            print(f"[PAYLOAD] ❌ Token {token} not found in MongoDB either")
            return None

        if time.time() > doc.get("expires", 0):
            print(f"[PAYLOAD] ❌ Token {token} found in MongoDB but EXPIRED")
            return None

        print(f"[PAYLOAD] ✅ Token {token} recovered from MongoDB")

        # Warm memory cache so next check is fast
        PAYLOAD_STORE[token] = {
            "uid":     doc["user_id"],
            "series":  doc["series"],
            "season":  doc["season"],
            "lang":    doc["lang"],
            "quality": doc["quality"],
            "expires": doc["expires"],
            "token":   token,
        }

        return (doc["user_id"], doc["series"], doc["season"],
                doc["lang"], doc["quality"])

    except Exception as e:
        print(f"[PAYLOAD] ❌ MongoDB fallback error: {e}")
        return None


# Keep old sync name as a no-op stub so nothing else breaks if imported
def decode_payload(payload: str):
    """
    DEPRECATED — use decode_payload_async() instead.
    This sync version cannot do the MongoDB fallback.
    Only checks memory. Returns None if token not in memory.
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
