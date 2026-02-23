"""
fsub.py — Force Subscribe Module
=================================
Supports 2 FSub slots. Each slot can be configured as:

  MODE A — "request"  : user sends a join REQUEST.
                        Bot records them in MongoDB.
                        File is delivered as soon as the record exists
                        (user stays PENDING in channel — no approval needed).

  MODE B — "invite"   : normal invite link.
                        Bot checks get_chat_member() to verify membership.

Both slots are independent and can mix modes (e.g. slot 1 = request, slot 2 = invite).
Each slot also supports:
  • A secondary (fallback) channel that activates when a count threshold is exceeded.
  • The secondary inherits the same mode as the primary.

FIX: Removed broken Motor/separate-client setup.
     Now reuses the existing synchronous PyMongo client from DB.database
     via asyncio.to_thread — same connection, no missing URI issues.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, ChatJoinRequest

from utils.cyber_font import BTN, UI
from DB.database import get_global_setting, set_global_setting, mongo_client


# ─────────────────────────────────────────────────────────────
# SHARED MONGO DB  (reuse existing sync client from database.py)
# ─────────────────────────────────────────────────────────────

_join_reqs_db = mongo_client["JoinReqs"]


# ─────────────────────────────────────────────────────────────
# MODULE-LEVEL STATE  (mutated at runtime by admin commands)
# ─────────────────────────────────────────────────────────────

# Slot 1 — primary
FSUB_CHANNEL1: int | None = None
FSUB_LINK1:    str | None = None
FSUB_MODE1:    str        = "request"   # "request" | "invite"

# Slot 1 — secondary / fallback
FSUB_CHANNEL1_2: int | None = None
FSUB_LINK1_2:    str | None = None

# Slot 2 — primary
FSUB_CHANNEL2: int | None = None
FSUB_LINK2:    str | None = None
FSUB_MODE2:    str        = "request"

# Slot 2 — secondary / fallback
FSUB_CHANNEL2_2: int | None = None
FSUB_LINK2_2:    str | None = None

# Thresholds & counters
COUNT_THRESHOLD1: int | None = None
COUNT_THRESHOLD2: int | None = None
_req_count1: int = 0
_req_count2: int = 0


# ─────────────────────────────────────────────────────────────
# COLLECTION HELPER
# ─────────────────────────────────────────────────────────────

def _col(slot: int):
    """Return the sync PyMongo collection for the given slot."""
    cid = FSUB_CHANNEL1 if slot == 1 else FSUB_CHANNEL2
    if not cid:
        return None
    return _join_reqs_db[str(cid)]


# ─────────────────────────────────────────────────────────────
# DB OPERATIONS  (sync functions wrapped with asyncio.to_thread)
# ─────────────────────────────────────────────────────────────

async def _add_req(slot: int, user_id: int, first_name: str, username: str):
    col = _col(slot)
    if col is None:
        print(f"[FSUB] _add_req: col is None for slot={slot}, channel not configured?")
        return

    def _sync():
        try:
            col.update_one(
                {"_id": int(user_id)},
                {"$setOnInsert": {
                    "_id":        int(user_id),
                    "user_id":    int(user_id),
                    "first_name": first_name,
                    "username":   username,
                    "date":       datetime.utcnow(),
                }},
                upsert=True
            )
            print(f"[FSUB] Recorded join request uid={user_id} slot={slot}")
        except Exception as e:
            print(f"[FSUB] _add_req DB error: {e}")

    await asyncio.to_thread(_sync)


async def _has_req(slot: int, user_id: int) -> bool:
    col = _col(slot)
    if col is None:
        print(f"[FSUB] _has_req: col is None for slot={slot}")
        return False

    def _sync():
        doc = col.find_one({"user_id": int(user_id)})
        print(f"[FSUB] _has_req uid={user_id} slot={slot} found={doc is not None}")
        return doc is not None

    return await asyncio.to_thread(_sync)


async def _count_reqs(slot: int) -> int:
    col = _col(slot)
    if col is None:
        return 0

    def _sync():
        return col.count_documents({})

    return await asyncio.to_thread(_sync)


async def _clear_reqs(slot: int):
    col = _col(slot)
    if col is None:
        return

    def _sync():
        col.delete_many({})

    await asyncio.to_thread(_sync)


async def _purge_user_req(slot: int, user_id: int):
    col = _col(slot)
    if col is None:
        return

    def _sync():
        col.delete_one({"user_id": int(user_id)})

    await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# SETTINGS  persist/load
# ─────────────────────────────────────────────────────────────

DB_KEY = "fsub_config_v2"


async def load_fsub_settings():
    global FSUB_CHANNEL1, FSUB_LINK1, FSUB_MODE1, FSUB_CHANNEL1_2, FSUB_LINK1_2
    global FSUB_CHANNEL2, FSUB_LINK2, FSUB_MODE2, FSUB_CHANNEL2_2, FSUB_LINK2_2
    global COUNT_THRESHOLD1, COUNT_THRESHOLD2, _req_count1, _req_count2

    d = await get_global_setting(DB_KEY) or {}

    FSUB_CHANNEL1   = d.get("ch1")
    FSUB_LINK1      = d.get("lk1")
    FSUB_MODE1      = d.get("mode1", "request")
    FSUB_CHANNEL1_2 = d.get("ch1_2")
    FSUB_LINK1_2    = d.get("lk1_2")

    FSUB_CHANNEL2   = d.get("ch2")
    FSUB_LINK2      = d.get("lk2")
    FSUB_MODE2      = d.get("mode2", "request")
    FSUB_CHANNEL2_2 = d.get("ch2_2")
    FSUB_LINK2_2    = d.get("lk2_2")

    COUNT_THRESHOLD1 = d.get("thr1")
    COUNT_THRESHOLD2 = d.get("thr2")
    _req_count1      = d.get("cnt1", 0)
    _req_count2      = d.get("cnt2", 0)

    print(f"[FSUB] Settings loaded: ch1={FSUB_CHANNEL1} mode1={FSUB_MODE1} | ch2={FSUB_CHANNEL2} mode2={FSUB_MODE2}")


async def save_fsub_settings():
    await set_global_setting(DB_KEY, {
        "ch1":   FSUB_CHANNEL1,   "lk1":   FSUB_LINK1,   "mode1": FSUB_MODE1,
        "ch1_2": FSUB_CHANNEL1_2, "lk1_2": FSUB_LINK1_2,
        "ch2":   FSUB_CHANNEL2,   "lk2":   FSUB_LINK2,   "mode2": FSUB_MODE2,
        "ch2_2": FSUB_CHANNEL2_2, "lk2_2": FSUB_LINK2_2,
        "thr1":  COUNT_THRESHOLD1, "thr2": COUNT_THRESHOLD2,
        "cnt1":  _req_count1,      "cnt2": _req_count2,
    })


# ─────────────────────────────────────────────────────────────
# CHECK FSub
# ─────────────────────────────────────────────────────────────

async def check_fsub(cyber, user_id: int) -> bool:
    """
    Returns True if the user passes FSub for EVERY active slot.

    request mode → checks MongoDB (user is still PENDING, that's fine)
    invite  mode → checks get_chat_member()
    """
    slots = [
        (FSUB_CHANNEL1, FSUB_MODE1, 1),
        (FSUB_CHANNEL2, FSUB_MODE2, 2),
    ]

    for ch, mode, slot in slots:
        if not ch:
            continue   # slot not configured

        if mode == "request":
            if not await _has_req(slot, user_id):
                return False

        else:  # "invite"
            try:
                member = await cyber.get_chat_member(ch, user_id)
                if member.status in ("left", "kicked"):
                    return False
            except Exception:
                return False

    return True


# ─────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────

def fsub_text() -> str:
    return UI(
        "⚠️ Access restricted.\n\n"
        "Please join the required channel(s) first.\n"
        "After joining, press ☘️ RETRY DOWNLOAD ☘️"
    )


def build_fsub_keyboard(bot_username: str, payload: str) -> InlineKeyboardMarkup:
    rows = []

    slots = [
        (FSUB_LINK1, FSUB_MODE1, "CHANNEL 1"),
        (FSUB_LINK2, FSUB_MODE2, "CHANNEL 2"),
    ]

    for link, mode, label in slots:
        if not link:
            continue
        emoji = "📢" if mode == "request" else "🔗"
        action = "REQUEST TO JOIN" if mode == "request" else "JOIN"
        rows.append([BTN(f"{emoji} {action} {label} {emoji}", url=link)])

    rows.append([
        BTN(
            "☘️ RETRY DOWNLOAD ☘️",
            url=f"https://t.me/{bot_username}?start={payload}"
        )
    ])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────
# THRESHOLD / CHANNEL ROTATION
# ─────────────────────────────────────────────────────────────

async def _rotate_slot(slot: int):
    """
    Switch primary → secondary when count > threshold.
    Clears the MongoDB collection after rotation.
    """
    import sys
    m = sys.modules[__name__]

    ch  = getattr(m, f"FSUB_CHANNEL{slot}")
    thr = getattr(m, f"COUNT_THRESHOLD{slot}")
    cnt = getattr(m, f"_req_count{slot}")
    ch2 = getattr(m, f"FSUB_CHANNEL{slot}_2")
    lk2 = getattr(m, f"FSUB_LINK{slot}_2")

    if not (thr and cnt > thr):
        return

    if ch2:
        setattr(m, f"FSUB_CHANNEL{slot}",   ch2)
        setattr(m, f"FSUB_LINK{slot}",      lk2)
        setattr(m, f"FSUB_CHANNEL{slot}_2", None)
        setattr(m, f"FSUB_LINK{slot}_2",    None)
        print(f"[FSUB] Slot {slot} rotated to secondary (cnt={cnt} > thr={thr})")
    else:
        setattr(m, f"FSUB_CHANNEL{slot}", None)
        setattr(m, f"FSUB_LINK{slot}",    None)
        print(f"[FSUB] Slot {slot} disabled — no secondary (cnt={cnt} > thr={thr})")

    setattr(m, f"_req_count{slot}", 0)
    await _clear_reqs(slot)
    await save_fsub_settings()


# ─────────────────────────────────────────────────────────────
# JOIN REQUEST HANDLER
# ─────────────────────────────────────────────────────────────

@Client.on_chat_join_request()
async def _on_join_request(bot, join_req: ChatJoinRequest):
    global _req_count1, _req_count2

    try:
        chat_id    = join_req.chat.id
        user       = join_req.from_user
        uid        = user.id
        first_name = user.first_name or ""
        username   = user.username   or ""

        print(f"[FSUB] Join request received: uid={uid} chat={chat_id} | ch1={FSUB_CHANNEL1} ch2={FSUB_CHANNEL2}")

        if chat_id == FSUB_CHANNEL1 and FSUB_MODE1 == "request":
            await _add_req(1, uid, first_name, username)
            _req_count1 += 1
            await _rotate_slot(1)

        elif chat_id == FSUB_CHANNEL2 and FSUB_MODE2 == "request":
            await _add_req(2, uid, first_name, username)
            _req_count2 += 1
            await _rotate_slot(2)

        else:
            print(f"[FSUB] Join request from chat {chat_id} doesn't match any configured slot — ignoring.")

    except Exception as e:
        print(f"[FSUB] JOIN REQUEST ERROR: {e}")


# ─────────────────────────────────────────────────────────────
# PUBLIC HELPERS  (used by admin commands in command.py)
# ─────────────────────────────────────────────────────────────

async def set_fsub_slot(
    slot: int,
    channel_id: int,
    link: str,
    mode: str,          # "request" | "invite"
    secondary: bool = False,
):
    """Set primary or secondary channel for a slot."""
    import sys
    m = sys.modules[__name__]

    suffix = f"{slot}_2" if secondary else str(slot)
    setattr(m, f"FSUB_CHANNEL{suffix}", channel_id)
    setattr(m, f"FSUB_LINK{suffix}",    link)
    if not secondary:
        setattr(m, f"FSUB_MODE{slot}", mode)
        setattr(m, f"_req_count{slot}", 0)

    await save_fsub_settings()


async def del_fsub_slot(slot: int, secondary: bool = False):
    """Clear primary or secondary channel for a slot."""
    import sys
    m = sys.modules[__name__]

    suffix = f"{slot}_2" if secondary else str(slot)
    old = getattr(m, f"FSUB_CHANNEL{suffix}")
    setattr(m, f"FSUB_CHANNEL{suffix}", None)
    setattr(m, f"FSUB_LINK{suffix}",    None)
    if not secondary:
        setattr(m, f"_req_count{slot}", 0)
        await _clear_reqs(slot)

    await save_fsub_settings()
    return old


async def set_threshold(slot: int, value: int):
    import sys
    m = sys.modules[__name__]
    setattr(m, f"COUNT_THRESHOLD{slot}", value)
    await save_fsub_settings()


async def purge_slot_reqs(slot: int):
    """Delete all join-request records for a slot and reset counter."""
    import sys
    m = sys.modules[__name__]
    await _clear_reqs(slot)
    setattr(m, f"_req_count{slot}", 0)
    await save_fsub_settings()


async def get_slot_info(bot, slot: int) -> dict:
    """Return a dict with all info for a slot (for /view_sub)."""
    import sys
    m = sys.modules[__name__]

    ch   = getattr(m, f"FSUB_CHANNEL{slot}")
    lk   = getattr(m, f"FSUB_LINK{slot}")
    mode = getattr(m, f"FSUB_MODE{slot}")
    ch2  = getattr(m, f"FSUB_CHANNEL{slot}_2")
    thr  = getattr(m, f"COUNT_THRESHOLD{slot}")
    cnt  = getattr(m, f"_req_count{slot}")

    title = None
    if ch:
        try:
            chat  = await bot.get_chat(ch)
            title = chat.title
        except Exception:
            title = "Unknown"

    db_count = await _count_reqs(slot) if (ch and mode == "request") else None

    return {
        "slot":     slot,
        "ch":       ch,
        "title":    title,
        "link":     lk,
        "mode":     mode,
        "ch2":      ch2,
        "threshold":thr,
        "counter":  cnt,
        "db_count": db_count,
    }
