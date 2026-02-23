import re
import time
import uuid

# =========================================================
# LINK PATTERNS
# =========================================================

PRIVATE_LINK_PATTERN = r"https://t\.me/c/(\d+)/(\d+)"
PUBLIC_LINK_PATTERN = r"https://t\.me/([A-Za-z0-9_]+)/(\d+)"
JOINCHAT_PATTERN = r"https://t\.me/\+([A-Za-z0-9_-]+)"


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
    chat2, last = parse_link(links[1])

    return chat1, start, last


# =========================================================
# SHORT TOKEN PAYLOAD SYSTEM
# =========================================================

PAYLOAD_STORE = {}

# TTL increased to 1 hour so users have plenty of time to join and retry.
# Previously 600s (10 min) was too short if the bot restarted or the user
# was slow to join.  TTL cleanup still runs automatically via _cleanup_payloads().
PAYLOAD_TTL = 3600  # 1 hour


def _cleanup_payloads():
    """Remove expired payloads."""
    now = time.time()
    expired = [
        token for token, data in PAYLOAD_STORE.items()
        if data["expires"] < now
    ]
    for token in expired:
        PAYLOAD_STORE.pop(token, None)


def encode_payload(series, season, lang, quality, uid):
    """
    Create short token instead of base64 payload.
    Safe for Telegram deep-link (max 64 chars).
    """
    _cleanup_payloads()

    token = uuid.uuid4().hex[:12]  # 12-char safe token

    PAYLOAD_STORE[token] = {
        "uid":     int(uid),
        "series":  series,
        "season":  int(season),
        "lang":    lang,
        "quality": quality,
        "expires": time.time() + PAYLOAD_TTL
    }

    return f"send_{token}"


def decode_payload(payload):
    """
    Decode short token payload.
    Returns: (uid, series, season, lang, quality)

    FIX: Token is NO LONGER deleted after first use.
    Previously the token was popped on first decode, which meant:
      1. User clicks link → fsub check fails → "Access Restricted" shown
      2. User joins channel → clicks RETRY button (same link)
      3. decode_payload called again → token already gone → returns None → blocked forever

    Now the token persists until TTL expiry (1 hour), so the user can
    retry as many times as needed after joining.
    """
    _cleanup_payloads()

    token = payload.replace("send_", "")
    data = PAYLOAD_STORE.get(token)

    if not data:
        return None

    # ✅ DO NOT pop the token here — let TTL handle cleanup.
    # This allows the RETRY button to work after the user joins the channel.

    return (
        data["uid"],
        data["series"],
        data["season"],
        data["lang"],
        data["quality"]
    )
