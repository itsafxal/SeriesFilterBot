"""
fsub.py — Force Subscribe Module (FIXED)
=========================================
Supports 2 FSub slots. Each slot can be configured as:

  MODE A — "request"  : user sends a join REQUEST.
                        Bot records them in MongoDB.
                        File is delivered as soon as the record exists
                        (user stays PENDING in channel — no approval needed).

  MODE B — "invite"   : normal invite link.
                        Bot checks get_chat_member() to verify membership.

FIXES APPLIED:
  1. _col() now correctly resolves channel ID at call time via getattr
  2. Settings are always saved/loaded correctly
  3. check_fsub logs every step for easy debugging
  4. _has_req and _add_req both use user_id field consistently
  5. On startup, if FSUB_CHANNEL is None, slot is skipped (not blocking)
  6. Removed all module-level import side effects that caused None states
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
# COLLECTION HELPER  — always reads current global var
# ─────────────────────────────────────────────────────────────

def _get_channel_id(slot: int) -> int | None:
    """Safely read the current channel ID for a slot from module globals."""
    import sys
    m = sys.modules[__name__]
    return getattr(m, f"FSUB_CHANNEL{slot}", None)


def _col(slot: int):
    """Return the sync PyMongo collection for the given slot."""
    cid = _get_channel_id(slot)
    if not cid:
        return None
    return _join_reqs_db[str(cid)]


# ─────────────────────────────────────────────────────────────
# DB OPERATIONS  (sync functions wrapped with asyncio.to_thread)
# ─────────────────────────────────────────────────────────────

async def _add_req(slot: int, user_id: int, first_name: str, username: str):
    col = _col(slot)
    if col is None:
        print(f"[FSUB] _add_req: col is None for slot={slot} — channel not configured?")
        return

    def _sync():
        try:
            col.update_one(
                {"user_id": int(user_id)},
                {"$setOnInsert": {
                    "_id":        int(user_id),
                    "user_id":    int(user_id),
                    "first_name": first_name,
                    "username":   username,
                    "date":       datetime.utcnow(),
                }},
                upsert=True
            )
            print(f"[FSUB] ✅ Recorded join request uid={user_id} slot={slot}")
        except Exception as e:
            print(f"[FSUB] ❌ _add_req DB error uid={user_id} slot={slot}: {e}")

    await asyncio.to_thread(_sync)


async def _has_req(slot: int, user_id: int) -> bool:
    """Check if a user has a recorded join request for the given slot."""
    col = _col(slot)
    if col is None:
        print(f"[FSUB] _has_req: col is None for slot={slot} — treating as NOT joined")
        return False

    def _sync():
        doc = col.find_one({"user_id": int(user_id)})
        found = doc is not None
        print(f"[FSUB] _has_req uid={user_id} slot={slot} found={found}")
        return found

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
        print(f"[FSUB] Cleared all join request records for slot={slot}")

    await asyncio.to_thread(_sync)


async def _purge_user_req(slot: int, user_id: int):
    col = _col(slot)
    if col is None:
        return

    def _sync():
        col.delete_one({"user_id": int(user_id)})
        print(f"[FSUB] Purged join request uid={user_id} slot={slot}")

    await asyncio.to_thread(_sync)


# ─────────────────────────────────────────────────────────────
# SETTINGS  persist/load
# ─────────────────────────────────────────────────────────────

DB_KEY = "fsub_config_v2"


async def load_fsub_settings():
    """Load FSub settings from MongoDB. Called once on bot startup."""
    global FSUB_CHANNEL1, FSUB_LINK1, FSUB_MODE1, FSUB_CHANNEL1_2, FSUB_LINK1_2
    global FSUB_CHANNEL2, FSUB_LINK2, FSUB_MODE2, FSUB_CHANNEL2_2, FSUB_LINK2_2
    global COUNT_THRESHOLD1, COUNT_THRESHOLD2, _req_count1, _req_count2

    try:
        d = await get_global_setting(DB_KEY) or {}
    except Exception as e:
        print(f"[FSUB] ❌ Failed to load settings from DB: {e}")
        d = {}

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

    print(
        f"[FSUB] ✅ Settings loaded:\n"
        f"         Slot1: ch={FSUB_CHANNEL1} mode={FSUB_MODE1} link={FSUB_LINK1}\n"
        f"         Slot2: ch={FSUB_CHANNEL2} mode={FSUB_MODE2} link={FSUB_LINK2}\n"
        f"         Thresholds: thr1={COUNT_THRESHOLD1} thr2={COUNT_THRESHOLD2}"
    )


async def save_fsub_settings():
    """Persist current FSub settings to MongoDB."""
    import sys
    m = sys.modules[__name__]

    data = {
        "ch1":   getattr(m, "FSUB_CHANNEL1",   None),
        "lk1":   getattr(m, "FSUB_LINK1",      None),
        "mode1": getattr(m, "FSUB_MODE1",       "request"),
        "ch1_2": getattr(m, "FSUB_CHANNEL1_2",  None),
        "lk1_2": getattr(m, "FSUB_LINK1_2",     None),
        "ch2":   getattr(m, "FSUB_CHANNEL2",    None),
        "lk2":   getattr(m, "FSUB_LINK2",       None),
        "mode2": getattr(m, "FSUB_MODE2",       "request"),
        "ch2_2": getattr(m, "FSUB_CHANNEL2_2",  None),
        "lk2_2": getattr(m, "FSUB_LINK2_2",     None),
        "thr1":  getattr(m, "COUNT_THRESHOLD1", None),
        "thr2":  getattr(m, "COUNT_THRESHOLD2", None),
        "cnt1":  getattr(m, "_req_count1",      0),
        "cnt2":  getattr(m, "_req_count2",      0),
    }

    try:
        await set_global_setting(DB_KEY, data)
        print(f"[FSUB] ✅ Settings saved: {data}")
    except Exception as e:
        print(f"[FSUB] ❌ Failed to save settings: {e}")


# ─────────────────────────────────────────────────────────────
# CHECK FSub — MAIN GATE
# ─────────────────────────────────────────────────────────────

async def check_fsub(cyber, user_id: int) -> bool:
    """
    Returns True if the user passes FSub for EVERY active slot.

    request mode → checks MongoDB (user is still PENDING, that's fine)
    invite  mode → checks get_chat_member()

    If a slot has no channel configured, it is skipped (not blocking).
    """
    import sys
    m = sys.modules[__name__]

    slots = [
        (getattr(m, "FSUB_CHANNEL1", None), getattr(m, "FSUB_MODE1", "request"), 1),
        (getattr(m, "FSUB_CHANNEL2", None), getattr(m, "FSUB_MODE2", "request"), 2),
    ]

    for ch, mode, slot in slots:
        if not ch:
            print(f"[FSUB] Slot {slot} not configured — skipping")
            continue

        print(f"[FSUB] Checking slot {slot}: ch={ch} mode={mode} uid={user_id}")

        if mode == "request":
            has = await _has_req(slot, user_id)
            if not has:
                print(f"[FSUB] ❌ User {user_id} has NOT joined slot {slot} (request mode)")
                return False
            print(f"[FSUB] ✅ User {user_id} passed slot {slot} (request mode)")

        else:  # "invite"
            try:
                member = await cyber.get_chat_member(ch, user_id)
                status = member.status.value if hasattr(member.status, "value") else str(member.status)
                print(f"[FSUB] Slot {slot} invite check: uid={user_id} status={status}")
                if status in ("left", "kicked", "ChatMemberStatus.LEFT", "ChatMemberStatus.BANNED"):
                    print(f"[FSUB] ❌ User {user_id} not in slot {slot} (invite mode, status={status})")
                    return False
                print(f"[FSUB] ✅ User {user_id} passed slot {slot} (invite mode)")
            except Exception as e:
                print(f"[FSUB] ❌ get_chat_member error slot={slot} uid={user_id}: {e}")
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
    import sys
    m = sys.modules[__name__]

    rows = []

    slots = [
        (getattr(m, "FSUB_LINK1", None), getattr(m, "FSUB_MODE1", "request"), "CHANNEL 1"),
        (getattr(m, "FSUB_LINK2", None), getattr(m, "FSUB_MODE2", "request"), "CHANNEL 2"),
    ]

    for link, mode, label in slots:
        if not link:
            continue
        emoji  = "📢" if mode == "request" else "🔗"
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
    """Switch primary → secondary when count > threshold. Clears records after rotation."""
    import sys
    m = sys.modules[__name__]

    thr = getattr(m, f"COUNT_THRESHOLD{slot}", None)
    cnt = getattr(m, f"_req_count{slot}", 0)
    ch2 = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
    lk2 = getattr(m, f"FSUB_LINK{slot}_2", None)

    if not thr or cnt <= thr:
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
    import sys
    m = sys.modules[__name__]

    try:
        chat_id    = join_req.chat.id
        user       = join_req.from_user
        uid        = user.id
        first_name = user.first_name or ""
        username   = user.username   or ""

        ch1 = getattr(m, "FSUB_CHANNEL1", None)
        ch2 = getattr(m, "FSUB_CHANNEL2", None)

        print(f"[FSUB] Join request: uid={uid} chat={chat_id} | configured ch1={ch1} ch2={ch2}")

        if ch1 and chat_id == ch1 and getattr(m, "FSUB_MODE1", "request") == "request":
            await _add_req(1, uid, first_name, username)
            m._req_count1 += 1
            await _rotate_slot(1)

        elif ch2 and chat_id == ch2 and getattr(m, "FSUB_MODE2", "request") == "request":
            await _add_req(2, uid, first_name, username)
            m._req_count2 += 1
            await _rotate_slot(2)

        else:
            print(f"[FSUB] Join request from chat {chat_id} doesn't match any configured slot — ignoring.")

    except Exception as e:
        print(f"[FSUB] ❌ JOIN REQUEST ERROR: {e}")


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

    if secondary:
        setattr(m, f"FSUB_CHANNEL{slot}_2", channel_id)
        setattr(m, f"FSUB_LINK{slot}_2",    link)
    else:
        setattr(m, f"FSUB_CHANNEL{slot}", channel_id)
        setattr(m, f"FSUB_LINK{slot}",    link)
        setattr(m, f"FSUB_MODE{slot}",    mode)
        setattr(m, f"_req_count{slot}",   0)

    await save_fsub_settings()
    print(f"[FSUB] Slot {slot} {'secondary' if secondary else 'primary'} set: ch={channel_id} mode={mode}")


async def del_fsub_slot(slot: int, secondary: bool = False) -> int | None:
    """Clear primary or secondary channel for a slot. Returns old channel ID."""
    import sys
    m = sys.modules[__name__]

    if secondary:
        old = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
        setattr(m, f"FSUB_CHANNEL{slot}_2", None)
        setattr(m, f"FSUB_LINK{slot}_2",    None)
    else:
        old = getattr(m, f"FSUB_CHANNEL{slot}", None)
        setattr(m, f"FSUB_CHANNEL{slot}", None)
        setattr(m, f"FSUB_LINK{slot}",    None)
        setattr(m, f"_req_count{slot}",   0)
        await _clear_reqs(slot)

    await save_fsub_settings()
    print(f"[FSUB] Slot {slot} {'secondary' if secondary else 'primary'} deleted (was ch={old})")
    return old


async def set_threshold(slot: int, value: int):
    """Set the request count threshold for automatic slot rotation."""
    import sys
    m = sys.modules[__name__]
    setattr(m, f"COUNT_THRESHOLD{slot}", value)
    await save_fsub_settings()
    print(f"[FSUB] Threshold for slot {slot} set to {value}")


async def purge_slot_reqs(slot: int):
    """Delete all join-request records for a slot and reset counter."""
    import sys
    m = sys.modules[__name__]
    await _clear_reqs(slot)
    setattr(m, f"_req_count{slot}", 0)
    await save_fsub_settings()
    print(f"[FSUB] Purged all records for slot {slot}")


async def get_slot_info(bot, slot: int) -> dict:
    """Return a dict with all info for a slot (for /view_sub)."""
    import sys
    m = sys.modules[__name__]

    ch   = getattr(m, f"FSUB_CHANNEL{slot}", None)
    lk   = getattr(m, f"FSUB_LINK{slot}",    None)
    mode = getattr(m, f"FSUB_MODE{slot}",    "request")
    ch2  = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
    thr  = getattr(m, f"COUNT_THRESHOLD{slot}", None)
    cnt  = getattr(m, f"_req_count{slot}",   0)

    title = None
    if ch:
        try:
            chat  = await bot.get_chat(ch)
            title = chat.title
        except Exception:
            title = "Unknown"

    db_count = await _count_reqs(slot) if (ch and mode == "request") else None

    return {
        "slot":      slot,
        "ch":        ch,
        "title":     title,
        "link":      lk,
        "mode":      mode,
        "ch2":       ch2,
        "threshold": thr,
        "counter":   cnt,
        "db_count":  db_count,
    }
