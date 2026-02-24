"""
fsub.py — BULLETPROOF REWRITE

THE REAL ROOT CAUSE (now fixed):
  In "request" mode, the bot only passes check_fsub() if it received
  the chat_join_request Telegram update and wrote it to MongoDB.

  This update is silently dropped when:
    - Bot lacks "Manage Invite Links" admin permission in the channel
    - The invite link was not created by this bot
    - Telegram fails to deliver the update (rare but happens)

  Result: User sends join request → Telegram never tells the bot →
  MongoDB stays empty → user clicks RETRY forever → always blocked.

FIX:
  check_fsub() now does a DUAL CHECK for "request" mode:
    1. Check MongoDB (fast, works normally)
    2. If not found → check Telegram API directly via get_chat_member()
       If user IS in the channel (any status except left/banned) → PASS
       and record them in DB for future fast lookups.

  This makes FSub work even when join_request events are dropped.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, ChatJoinRequest
from pyrogram.enums import ChatMemberStatus

from utils.cyber_font import BTN, UI
from DB.database import get_global_setting, set_global_setting, mongo_client


_join_reqs_db = mongo_client["JoinReqs"]

# ── module globals ────────────────────────────────────────────
FSUB_CHANNEL1: int | None = None
FSUB_LINK1:    str | None = None
FSUB_MODE1:    str        = "request"
FSUB_CHANNEL1_2: int | None = None
FSUB_LINK1_2:    str | None = None

FSUB_CHANNEL2: int | None = None
FSUB_LINK2:    str | None = None
FSUB_MODE2:    str        = "request"
FSUB_CHANNEL2_2: int | None = None
FSUB_LINK2_2:    str | None = None

COUNT_THRESHOLD1: int | None = None
COUNT_THRESHOLD2: int | None = None
_req_count1: int = 0
_req_count2: int = 0


def _m():
    import sys
    return sys.modules[__name__]


def _get_ch(slot: int) -> int | None:
    return getattr(_m(), f"FSUB_CHANNEL{slot}", None)


def _col(slot: int):
    cid = _get_ch(slot)
    return _join_reqs_db[str(cid)] if cid else None


# ── DB ops ────────────────────────────────────────────────────

async def _add_req(slot: int, user_id: int, first_name: str = "", username: str = ""):
    col = _col(slot)
    if col is None:
        return
    def _sync():
        try:
            col.update_one(
                {"user_id": int(user_id)},
                {"$setOnInsert": {
                    "_id": int(user_id), "user_id": int(user_id),
                    "first_name": first_name, "username": username,
                    "date": datetime.utcnow(),
                }},
                upsert=True
            )
            print(f"[FSUB] DB write: uid={user_id} slot={slot} col={col.name}")
        except Exception as e:
            print(f"[FSUB] _add_req error: {e}")
    await asyncio.to_thread(_sync)


async def _has_req(slot: int, user_id: int) -> bool:
    col = _col(slot)
    if col is None:
        print(f"[FSUB] _has_req: col=None slot={slot} ch={_get_ch(slot)}")
        return False
    def _sync():
        doc = col.find_one({"user_id": int(user_id)})
        print(f"[FSUB] _has_req: uid={user_id} slot={slot} col={col.name} found={doc is not None}")
        return doc is not None
    return await asyncio.to_thread(_sync)


async def _count_reqs(slot: int) -> int:
    col = _col(slot)
    if col is None:
        return 0
    return await asyncio.to_thread(lambda: col.count_documents({}))


async def _clear_reqs(slot: int):
    col = _col(slot)
    if col is None:
        return
    await asyncio.to_thread(lambda: col.delete_many({}))


async def _purge_user_req(slot: int, user_id: int):
    col = _col(slot)
    if col is None:
        return
    await asyncio.to_thread(lambda: col.delete_one({"user_id": int(user_id)}))


# ── settings ──────────────────────────────────────────────────

DB_KEY = "fsub_config_v2"


async def load_fsub_settings():
    global FSUB_CHANNEL1, FSUB_LINK1, FSUB_MODE1, FSUB_CHANNEL1_2, FSUB_LINK1_2
    global FSUB_CHANNEL2, FSUB_LINK2, FSUB_MODE2, FSUB_CHANNEL2_2, FSUB_LINK2_2
    global COUNT_THRESHOLD1, COUNT_THRESHOLD2, _req_count1, _req_count2

    try:
        d = await get_global_setting(DB_KEY) or {}
    except Exception as e:
        print(f"[FSUB] load error: {e}")
        d = {}

    FSUB_CHANNEL1    = d.get("ch1")
    FSUB_LINK1       = d.get("lk1")
    FSUB_MODE1       = d.get("mode1", "request")
    FSUB_CHANNEL1_2  = d.get("ch1_2")
    FSUB_LINK1_2     = d.get("lk1_2")
    FSUB_CHANNEL2    = d.get("ch2")
    FSUB_LINK2       = d.get("lk2")
    FSUB_MODE2       = d.get("mode2", "request")
    FSUB_CHANNEL2_2  = d.get("ch2_2")
    FSUB_LINK2_2     = d.get("lk2_2")
    COUNT_THRESHOLD1 = d.get("thr1")
    COUNT_THRESHOLD2 = d.get("thr2")
    _req_count1      = d.get("cnt1", 0)
    _req_count2      = d.get("cnt2", 0)

    print(f"[FSUB] Loaded: ch1={FSUB_CHANNEL1} mode1={FSUB_MODE1} | ch2={FSUB_CHANNEL2} mode2={FSUB_MODE2}")


async def save_fsub_settings():
    m = _m()
    data = {
        "ch1":   getattr(m, "FSUB_CHANNEL1",    None),
        "lk1":   getattr(m, "FSUB_LINK1",       None),
        "mode1": getattr(m, "FSUB_MODE1",        "request"),
        "ch1_2": getattr(m, "FSUB_CHANNEL1_2",  None),
        "lk1_2": getattr(m, "FSUB_LINK1_2",     None),
        "ch2":   getattr(m, "FSUB_CHANNEL2",     None),
        "lk2":   getattr(m, "FSUB_LINK2",        None),
        "mode2": getattr(m, "FSUB_MODE2",        "request"),
        "ch2_2": getattr(m, "FSUB_CHANNEL2_2",  None),
        "lk2_2": getattr(m, "FSUB_LINK2_2",     None),
        "thr1":  getattr(m, "COUNT_THRESHOLD1",  None),
        "thr2":  getattr(m, "COUNT_THRESHOLD2",  None),
        "cnt1":  getattr(m, "_req_count1",       0),
        "cnt2":  getattr(m, "_req_count2",       0),
    }
    try:
        await set_global_setting(DB_KEY, data)
        print(f"[FSUB] Saved settings.")
    except Exception as e:
        print(f"[FSUB] Save error: {e}")


# ── Telegram API member check ─────────────────────────────────

async def _is_member_via_api(cyber, channel_id: int, user_id: int) -> bool:
    """Directly check if user is in channel via Telegram API."""
    try:
        member = await cyber.get_chat_member(channel_id, user_id)
        status = member.status
        print(f"[FSUB] API: uid={user_id} ch={channel_id} status={status}")
        return status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,
        )
    except Exception as e:
        err = str(e).lower()
        print(f"[FSUB] API error: uid={user_id} ch={channel_id} err={e}")
        # "user_not_participant" = not joined. Any other error = assume not joined.
        return False


# ── MAIN CHECK — DUAL CHECK ───────────────────────────────────

async def check_fsub(cyber, user_id: int) -> bool:
    """
    Returns True if user passes all active slots.

    request mode: checks MongoDB FIRST, then falls back to Telegram API.
    invite mode:  checks Telegram API only.

    The API fallback is the KEY FIX — it makes FSub work even when
    Telegram never delivered the join_request event to the bot.
    """
    m = _m()
    slots = [
        (getattr(m, "FSUB_CHANNEL1", None), getattr(m, "FSUB_MODE1", "request"), 1),
        (getattr(m, "FSUB_CHANNEL2", None), getattr(m, "FSUB_MODE2", "request"), 2),
    ]

    for ch, mode, slot in slots:
        if not ch:
            print(f"[FSUB] Slot {slot} not configured — skip")
            continue

        print(f"[FSUB] check_fsub: uid={user_id} slot={slot} ch={ch} mode={mode}")

        if mode == "request":
            # Step 1: fast MongoDB check
            if await _has_req(slot, user_id):
                print(f"[FSUB] ✅ uid={user_id} slot={slot}: pass (DB record)")
                continue

            # Step 2: API fallback — covers dropped join_request events
            print(f"[FSUB] uid={user_id} not in DB, trying API fallback...")
            if await _is_member_via_api(cyber, ch, user_id):
                print(f"[FSUB] ✅ uid={user_id} slot={slot}: pass (API), recording in DB")
                await _add_req(slot, user_id)
                continue

            print(f"[FSUB] ❌ uid={user_id} slot={slot}: FAIL (not in DB, not in channel)")
            return False

        else:  # invite mode
            if await _is_member_via_api(cyber, ch, user_id):
                print(f"[FSUB] ✅ uid={user_id} slot={slot}: pass (invite API)")
                continue
            print(f"[FSUB] ❌ uid={user_id} slot={slot}: FAIL (invite mode)")
            return False

    return True


# ── UI ────────────────────────────────────────────────────────

def fsub_text() -> str:
    return UI(
        "⚠️ Access restricted.\n\n"
        "Please join the required channel(s) first.\n"
        "After joining, press ☘️ RETRY DOWNLOAD ☘️"
    )


def build_fsub_keyboard(bot_username: str, payload: str) -> InlineKeyboardMarkup:
    m = _m()
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
    rows.append([BTN("☘️ RETRY DOWNLOAD ☘️", url=f"https://t.me/{bot_username}?start={payload}")])
    return InlineKeyboardMarkup(rows)


# ── rotation ──────────────────────────────────────────────────

async def _rotate_slot(slot: int):
    m = _m()
    thr = getattr(m, f"COUNT_THRESHOLD{slot}", None)
    cnt = getattr(m, f"_req_count{slot}", 0)
    if not thr or cnt <= thr:
        return
    ch2 = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
    lk2 = getattr(m, f"FSUB_LINK{slot}_2", None)
    if ch2:
        setattr(m, f"FSUB_CHANNEL{slot}",   ch2)
        setattr(m, f"FSUB_LINK{slot}",      lk2)
        setattr(m, f"FSUB_CHANNEL{slot}_2", None)
        setattr(m, f"FSUB_LINK{slot}_2",    None)
    else:
        setattr(m, f"FSUB_CHANNEL{slot}", None)
        setattr(m, f"FSUB_LINK{slot}",    None)
    setattr(m, f"_req_count{slot}", 0)
    await _clear_reqs(slot)
    await save_fsub_settings()


# ── join request handler ──────────────────────────────────────

@Client.on_chat_join_request()
async def _on_join_request(bot, join_req: ChatJoinRequest):
    m = _m()
    try:
        chat_id    = join_req.chat.id
        uid        = join_req.from_user.id
        first_name = join_req.from_user.first_name or ""
        username   = join_req.from_user.username   or ""
        ch1 = getattr(m, "FSUB_CHANNEL1", None)
        ch2 = getattr(m, "FSUB_CHANNEL2", None)
        print(f"[FSUB] JoinRequest: uid={uid} chat={chat_id} ch1={ch1} ch2={ch2}")
        if ch1 and chat_id == ch1 and getattr(m, "FSUB_MODE1", "request") == "request":
            await _add_req(1, uid, first_name, username)
            m._req_count1 += 1
            await _rotate_slot(1)
        elif ch2 and chat_id == ch2 and getattr(m, "FSUB_MODE2", "request") == "request":
            await _add_req(2, uid, first_name, username)
            m._req_count2 += 1
            await _rotate_slot(2)
        else:
            print(f"[FSUB] JoinRequest from unregistered chat {chat_id}")
    except Exception as e:
        print(f"[FSUB] JOIN REQUEST ERROR: {e}")


# ── public helpers ────────────────────────────────────────────

async def set_fsub_slot(slot: int, channel_id: int, link: str, mode: str, secondary: bool = False):
    m = _m()
    if secondary:
        setattr(m, f"FSUB_CHANNEL{slot}_2", channel_id)
        setattr(m, f"FSUB_LINK{slot}_2",    link)
    else:
        setattr(m, f"FSUB_CHANNEL{slot}", channel_id)
        setattr(m, f"FSUB_LINK{slot}",    link)
        setattr(m, f"FSUB_MODE{slot}",    mode)
        setattr(m, f"_req_count{slot}",   0)
    await save_fsub_settings()


async def del_fsub_slot(slot: int, secondary: bool = False) -> int | None:
    m = _m()
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
    return old


async def set_threshold(slot: int, value: int):
    setattr(_m(), f"COUNT_THRESHOLD{slot}", value)
    await save_fsub_settings()


async def purge_slot_reqs(slot: int):
    await _clear_reqs(slot)
    setattr(_m(), f"_req_count{slot}", 0)
    await save_fsub_settings()


async def get_slot_info(bot, slot: int) -> dict:
    m = _m()
    ch   = getattr(m, f"FSUB_CHANNEL{slot}", None)
    lk   = getattr(m, f"FSUB_LINK{slot}",    None)
    mode = getattr(m, f"FSUB_MODE{slot}",    "request")
    ch2  = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
    thr  = getattr(m, f"COUNT_THRESHOLD{slot}", None)
    cnt  = getattr(m, f"_req_count{slot}",   0)
    title = None
    if ch:
        try:
            title = (await bot.get_chat(ch)).title
        except Exception:
            title = "Unknown"
    db_count = await _count_reqs(slot) if (ch and mode == "request") else None
    return {"slot": slot, "ch": ch, "title": title, "link": lk,
            "mode": mode, "ch2": ch2, "threshold": thr, "counter": cnt, "db_count": db_count}
