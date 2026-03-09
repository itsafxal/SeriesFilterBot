"""
fsub.py — COMPLETE REWRITE (DEFINITIVE)
=========================================

SUPPORTED CONFIGURATIONS:
  • No FSub         → always pass, deliver immediately
  • Single slot     → 1 channel (request OR invite mode)
  • Double slot     → 2 channels (each independently request OR invite)

MODES PER SLOT:
  • "request" → user sends join request → auto-deliver instantly on request
  • "invite"  → user must actually JOIN the channel → auto-deliver on join
                (detected via Telegram member API poll on /start click)

RETRY LOGIC (FIXED):
  • Retry button appears after delivery for 60 seconds
  • Clicking retry while sender is already running → silently ignored
  • Clicking retry after sender is done → re-delivers (user genuinely missing files)
  • Delivery lock per uid stored in _DELIVERY_LOCK set — prevents true duplicates

AUTO-DELIVER TRIGGERS:
  1. request mode → on_chat_join_request event fires → auto-deliver
  2. invite  mode → user clicks /start link → get_slot_statuses checks API → if joined → deliver
     (no join_request event fires for invite links — Telegram doesn't send that event)

GATE MESSAGE (the "join our channel" message):
  • Sent when user clicks Download but fails FSub
  • Deleted automatically when all slots are satisfied
  • Updated keyboard in real-time as user joins each slot

FIXES IN THIS VERSION:
  • Bootstrap fallback: if DB returns ch1=None, seeds from config.py values.
    This fixes "JoinRequest from unknown chat — ignored" on fresh deployments.
  • Bootstrap only seeds memory — does NOT overwrite existing DB config.
  • Seed is saved to DB on first successful load so future restarts auto-load it.
  • FloodWait in _is_member_via_api now retries with proper backoff instead of
    returning False (which would wrongly fail FSub checks during rate-limit windows).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from pyrogram import Client, filters as pyro_filters
from pyrogram.types import InlineKeyboardMarkup, ChatJoinRequest, ChatMemberUpdated, Message
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait

from pyrogram.types import InlineKeyboardButton
from DB.database import get_global_setting, set_global_setting, mongo_client
from utils.cyber_delete import delete_message_later


_join_reqs_db = mongo_client["JoinReqs"]

# ── Module-level FSub config ──────────────────────────────────
FSUB_CHANNEL1:   int | None = None
FSUB_LINK1:      str | None = None
FSUB_MODE1:      str        = "request"
FSUB_CHANNEL1_2: int | None = None
FSUB_LINK1_2:    str | None = None

FSUB_CHANNEL2:   int | None = None
FSUB_LINK2:      str | None = None
FSUB_MODE2:      str        = "request"
FSUB_CHANNEL2_2: int | None = None
FSUB_LINK2_2:    str | None = None

COUNT_THRESHOLD1: int | None = None
COUNT_THRESHOLD2: int | None = None
_req_count1: int = 0
_req_count2: int = 0

# ── In-memory stores ──────────────────────────────────────────
# uid → {msg, payload}
_PENDING_FSUB: dict[int, dict] = {}

# uid → True  (set while delivery is running OR just completed)
_DELIVERY_LOCK: set[int] = set()


def _m():
    import sys
    return sys.modules[__name__]


def _get_ch(slot: int) -> int | None:
    return getattr(_m(), f"FSUB_CHANNEL{slot}", None)


def _col(slot: int):
    cid = _get_ch(slot)
    return _join_reqs_db[str(cid)] if cid else None


# ═══════════════════════════════════════════════════════════════
# DELIVERY LOCK — prevents duplicate sends
# ═══════════════════════════════════════════════════════════════

def acquire_delivery_lock(uid: int) -> bool:
    if uid in _DELIVERY_LOCK:
        return False
    _DELIVERY_LOCK.add(uid)
    return True


def release_delivery_lock(uid: int):
    _DELIVERY_LOCK.discard(uid)


def is_delivery_locked(uid: int) -> bool:
    return uid in _DELIVERY_LOCK


# ═══════════════════════════════════════════════════════════════
# DB OPS (join request records)
# ═══════════════════════════════════════════════════════════════

async def _add_req(slot: int, user_id: int, first_name: str = "", username: str = ""):
    col = _col(slot)
    if col is None:
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
        except Exception as e:
            print(f"[FSUB] _add_req error: {e}")
    await asyncio.to_thread(_sync)


async def _has_req(slot: int, user_id: int) -> bool:
    col = _col(slot)
    if col is None:
        return False
    return await asyncio.to_thread(
        lambda: col.find_one({"user_id": int(user_id)}) is not None
    )


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


# ═══════════════════════════════════════════════════════════════
# SETTINGS
# ═══════════════════════════════════════════════════════════════

DB_KEY = "fsub_config_v2"


async def load_fsub_settings():
    """
    Load FSub config from MongoDB.

    BOOTSTRAP FALLBACK (fix for "JoinRequest from unknown chat — ignored"):
    If MongoDB returns no config (ch1=None), we seed from config.py values
    (FSUB_CHANNEL1_ID, FSUB_CHANNEL1_LINK, FSUB_MODE1).
    This lets the bot work on first boot without needing to configure
    FSub via admin settings first.

    The seeded config is saved back to MongoDB so future restarts load
    it automatically and the bootstrap is no longer needed.
    """
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

    # ── BOOTSTRAP FALLBACK ────────────────────────────────────
    # If no channel was loaded from DB, try to seed from config.py.
    # This fixes the "ignored" join requests on first-boot / mis-configured deployments.
    _bootstrapped = False

    if FSUB_CHANNEL1 is None:
        try:
            from config import FSUB_CHANNEL1_ID, FSUB_CHANNEL1_LINK, FSUB_MODE1 as _mode1
            if FSUB_CHANNEL1_ID:
                FSUB_CHANNEL1 = int(FSUB_CHANNEL1_ID)
                FSUB_LINK1    = FSUB_CHANNEL1_LINK or None
                FSUB_MODE1    = _mode1 or "request"
                _bootstrapped = True
                print(
                    f"[FSUB] Bootstrap: seeded slot1 from config.py → "
                    f"ch={FSUB_CHANNEL1} mode={FSUB_MODE1}"
                )
        except (ImportError, AttributeError, ValueError) as e:
            print(f"[FSUB] Bootstrap slot1 skipped: {e}")

    if FSUB_CHANNEL2 is None:
        try:
            from config import FSUB_CHANNEL2_ID, FSUB_CHANNEL2_LINK, FSUB_MODE2 as _mode2
            if FSUB_CHANNEL2_ID:
                FSUB_CHANNEL2 = int(FSUB_CHANNEL2_ID)
                FSUB_LINK2    = FSUB_CHANNEL2_LINK or None
                FSUB_MODE2    = _mode2 or "request"
                _bootstrapped = True
                print(
                    f"[FSUB] Bootstrap: seeded slot2 from config.py → "
                    f"ch={FSUB_CHANNEL2} mode={FSUB_MODE2}"
                )
        except (ImportError, AttributeError, ValueError) as e:
            print(f"[FSUB] Bootstrap slot2 skipped: {e}")

    # Save bootstrap config to DB so future restarts skip this fallback
    if _bootstrapped:
        try:
            await save_fsub_settings()
            print("[FSUB] Bootstrap config persisted to MongoDB.")
        except Exception as e:
            print(f"[FSUB] Bootstrap save error (non-fatal): {e}")

    print(
        f"[FSUB] Loaded: ch1={FSUB_CHANNEL1} mode1={FSUB_MODE1} "
        f"| ch2={FSUB_CHANNEL2} mode2={FSUB_MODE2}"
    )


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
    except Exception as e:
        print(f"[FSUB] Save error: {e}")


# ═══════════════════════════════════════════════════════════════
# TELEGRAM MEMBER CHECK
# FIX: retry on FloodWait instead of silently returning False,
#      which would cause FSub to wrongly deny users during rate-limit windows.
# ═══════════════════════════════════════════════════════════════

async def _is_member_via_api(cyber, channel_id: int, user_id: int) -> bool:
    """
    Check if user is a member of channel_id.
    Retries once on FloodWait with the required delay.
    Returns False only on genuine non-member status or unrecoverable error.
    """
    for attempt in range(2):
        try:
            member = await cyber.get_chat_member(channel_id, user_id)
            return member.status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
                ChatMemberStatus.RESTRICTED,
            )
        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            print(f"[FSUB] FloodWait {wait}s checking uid={user_id} ch={channel_id} — waiting...")
            await asyncio.sleep(wait + 1)
            # retry once after waiting
        except Exception as e:
            err = str(e)
            # USER_NOT_PARTICIPANT is a normal non-member response — not an error
            if "USER_NOT_PARTICIPANT" in err or "user not found" in err.lower():
                return False
            print(f"[FSUB] API error uid={user_id} ch={channel_id}: {e}")
            return False
    # second attempt failed
    return False


# ═══════════════════════════════════════════════════════════════
# SLOT STATUS
# ═══════════════════════════════════════════════════════════════

async def get_slot_statuses(cyber, user_id: int) -> list[dict]:
    m      = _m()
    result = []

    configured = [
        (getattr(m, "FSUB_CHANNEL1", None), getattr(m, "FSUB_LINK1", None), getattr(m, "FSUB_MODE1", "request"), 1),
        (getattr(m, "FSUB_CHANNEL2", None), getattr(m, "FSUB_LINK2", None), getattr(m, "FSUB_MODE2", "request"), 2),
    ]

    for ch, link, mode, slot in configured:
        if not ch:
            continue   # slot not configured — skip entirely

        is_member = await _is_member_via_api(cyber, ch, user_id)

        if mode == "request":
            in_db = await _has_req(slot, user_id)
            # Auto-record if they're already a real member (fast path for returning users)
            if is_member and not in_db:
                await _add_req(slot, user_id)
                in_db = True
            joined    = is_member
            requested = in_db and not is_member
        else:
            # invite mode — only actual membership counts
            joined    = is_member
            requested = False

        result.append({
            "slot":      slot,
            "ch":        ch,
            "link":      link,
            "mode":      mode,
            "joined":    joined,
            "requested": requested,
        })

    return result


def _slot_passes(s: dict) -> bool:
    """Returns True if this single slot is satisfied."""
    if s["mode"] == "request":
        return s["joined"] or s["requested"]
    else:  # invite
        return s["joined"]


def all_slots_passed(statuses: list[dict]) -> bool:
    """
    True if ALL configured slots are satisfied.
    If no slots configured → True (no restriction).
    """
    if not statuses:
        return True
    return all(_slot_passes(s) for s in statuses)


# ═══════════════════════════════════════════════════════════════
# MAIN FSUB CHECK (for external callers)
# ═══════════════════════════════════════════════════════════════

async def check_fsub(cyber, user_id: int) -> bool:
    statuses = await get_slot_statuses(cyber, user_id)
    result   = all_slots_passed(statuses)
    print(f"[FSUB] check_fsub uid={user_id} → {'PASS' if result else 'FAIL'} statuses={[(s['slot'],s['joined'],s['requested']) for s in statuses]}")
    return result


# ═══════════════════════════════════════════════════════════════
# KEYBOARD BUILDER
# ═══════════════════════════════════════════════════════════════

def build_fsub_keyboard(
    bot_username: str,
    payload: str,
    statuses: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    m    = _m()
    rows = []

    if statuses is None:
        raw_slots = [
            (getattr(m, "FSUB_LINK1", None), getattr(m, "FSUB_MODE1", "request"), 1),
            (getattr(m, "FSUB_LINK2", None), getattr(m, "FSUB_MODE2", "request"), 2),
        ]
        for link, mode, slot in raw_slots:
            if not link:
                continue
            icon  = "📢" if mode == "request" else "🔗"
            label = "Request to Join" if mode == "request" else "Join Channel"
            rows.append([InlineKeyboardButton(f"{icon}  Channel {slot}  —  {label}", url=link)])
        return InlineKeyboardMarkup(rows)

    # Per-status view — only show unsatisfied slots
    for s in statuses:
        if _slot_passes(s):
            continue
        link = s["link"]
        mode = s["mode"]
        slot = s["slot"]
        if link:
            icon  = "📢" if mode == "request" else "🔗"
            label = "Request to Join" if mode == "request" else "Join Channel"
            rows.append([InlineKeyboardButton(f"{icon}  Channel {slot}  —  {label}", url=link)])

    return InlineKeyboardMarkup(rows)


def fsub_text(statuses: list[dict] | None = None) -> str:
    if not statuses:
        return (
            "🔒 <b>Members Only</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            "Join the required channel(s) below to access your files.\n\n"
            "<i>Your files will be delivered automatically after joining.</i>"
        )

    pending   = [s for s in statuses if not _slot_passes(s)]
    satisfied = [s for s in statuses if _slot_passes(s)]

    lines = [
        "🔒 <b>Members Only</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
    ]

    if pending:
        count = len(pending)
        lines.append(f"Join <b>{count}</b> more channel{'s' if count > 1 else ''} to unlock your files.")
        lines.append("")
        lines.append("<i>Files will be sent automatically once you're done.</i>")

    if satisfied:
        lines.append("")
        lines.append(f"✅  <b>{len(satisfied)}</b> channel{'s' if len(satisfied) > 1 else ''} completed.")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# PENDING FSUB REGISTRY
# ═══════════════════════════════════════════════════════════════

def register_pending_fsub(uid: int, msg: Message, payload: str):
    old = _PENDING_FSUB.get(uid)
    if old:
        try:
            asyncio.create_task(old["msg"].delete())
        except Exception:
            pass
    _PENDING_FSUB[uid] = {"msg": msg, "payload": payload}


def clear_pending_fsub(uid: int):
    _PENDING_FSUB.pop(uid, None)


# ═══════════════════════════════════════════════════════════════
# CORE DELIVERY HELPER
# ═══════════════════════════════════════════════════════════════

async def do_deliver(cyber, uid: int, payload: str, source: str = "unknown") -> bool:
    if not acquire_delivery_lock(uid):
        print(f"[FSUB] do_deliver uid={uid} source={source} → LOCKED (delivery in progress)")
        return False

    print(f"[FSUB] do_deliver uid={uid} source={source} → lock acquired")

    try:
        from utils.cyber_filter import decode_payload_async
        decoded = await decode_payload_async(payload)

        if not decoded:
            release_delivery_lock(uid)
            await cyber.send_message(
                uid,
                "⏳ <b>Link Expired</b>\n"
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                "This download link has expired.\n"
                "<i>Please search for the series again to get a fresh link.</i>",
                parse_mode="html",
            )
            return False

        owner, series, season, lang, quality = decoded

        if int(owner) != uid:
            release_delivery_lock(uid)
            await cyber.send_message(
            uid,
            "⛔ <b>Access Denied</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            "<i>This link belongs to another user.</i>",
            parse_mode="html",
        )
            return False

        from CyberTG.files import set_session, deliver_files_direct
        from pyrogram import enums as _enums

        set_session(uid, {
            "series":   series,
            "season":   int(season),
            "language": lang,
            "quality":  quality,
        })

        async def _after_delivery():
            release_delivery_lock(uid)
            print(f"[FSUB] delivery done uid={uid} — lock released")

        await deliver_files_direct(cyber, uid, on_complete=_after_delivery)
        return True

    except Exception as e:
        print(f"[FSUB] do_deliver error uid={uid}: {e}")
        release_delivery_lock(uid)
        return False


# ═══════════════════════════════════════════════════════════════
# AUTO-DELIVER AFTER JOIN/REQUEST
# ═══════════════════════════════════════════════════════════════

async def _try_auto_deliver(cyber, uid: int):
    pending = _PENDING_FSUB.get(uid)
    if not pending:
        return

    msg     = pending["msg"]
    payload = pending["payload"]

    statuses = await get_slot_statuses(cyber, uid)
    done     = all_slots_passed(statuses)

    if done:
        try:
            await msg.delete()
        except Exception:
            pass
        clear_pending_fsub(uid)

        await do_deliver(cyber, uid, payload, source="auto_deliver")

    else:
        try:
            bot    = await cyber.get_me()
            new_kb = build_fsub_keyboard(bot.username, payload, statuses)
            await msg.edit_reply_markup(new_kb)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# ROTATION
# ═══════════════════════════════════════════════════════════════

async def _rotate_slot(slot: int):
    m   = _m()
    thr = getattr(m, f"COUNT_THRESHOLD{slot}", None)
    cnt = getattr(m, f"_req_count{slot}", 0)
    if not thr or cnt <= thr:
        return
    ch2 = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
    lk2 = getattr(m, f"FSUB_LINK{slot}_2",    None)
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


# ═══════════════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════════════

def register_fsub_handlers(cyber: Client):
    """Call BEFORE cyber.start() in main.py."""

    @cyber.on_chat_join_request()
    async def _on_join_request(bot, join_req: ChatJoinRequest):
        m          = _m()
        chat_id    = join_req.chat.id
        uid        = join_req.from_user.id
        first_name = join_req.from_user.first_name or ""
        username   = join_req.from_user.username   or ""

        ch1 = getattr(m, "FSUB_CHANNEL1", None)
        ch2 = getattr(m, "FSUB_CHANNEL2", None)

        print(f"[FSUB] JoinRequest uid={uid} chat={chat_id}")

        try:
            if ch1 and chat_id == ch1 and getattr(m, "FSUB_MODE1", "request") == "request":
                await _add_req(1, uid, first_name, username)
                m._req_count1 += 1
                await _rotate_slot(1)
                print(f"[FSUB] Recorded slot=1 uid={uid}")

            elif ch2 and chat_id == ch2 and getattr(m, "FSUB_MODE2", "request") == "request":
                await _add_req(2, uid, first_name, username)
                m._req_count2 += 1
                await _rotate_slot(2)
                print(f"[FSUB] Recorded slot=2 uid={uid}")

            else:
                print(f"[FSUB] JoinRequest from unknown chat {chat_id} — ignored")
                return

            asyncio.create_task(_try_auto_deliver(cyber, uid))

        except Exception as e:
            print(f"[FSUB] JOIN REQUEST ERROR: {e}")

    @cyber.on_chat_member_updated()
    async def _on_member_joined(bot, update: ChatMemberUpdated):
        m = _m()

        new_status = update.new_chat_member
        if not new_status:
            return

        joined_statuses = (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
        if new_status.status not in joined_statuses:
            return

        try:
            uid     = new_status.user.id if new_status.user else None
            chat_id = update.chat.id     if update.chat     else None
        except Exception:
            return

        if not uid or not chat_id:
            return

        try:
            me = await bot.get_me()
            if uid == me.id:
                return
        except Exception:
            pass

        print(f"[FSUB] MemberJoined uid={uid} chat={chat_id}")

        ch1   = getattr(m, "FSUB_CHANNEL1", None)
        mode1 = getattr(m, "FSUB_MODE1",    "request")
        ch2   = getattr(m, "FSUB_CHANNEL2", None)
        mode2 = getattr(m, "FSUB_MODE2",    "request")

        matched = False

        if ch1 and chat_id == ch1 and mode1 == "invite":
            print(f"[FSUB] uid={uid} joined invite-mode slot=1")
            matched = True
        elif ch2 and chat_id == ch2 and mode2 == "invite":
            print(f"[FSUB] uid={uid} joined invite-mode slot=2")
            matched = True

        if not matched:
            return

        if uid not in _PENDING_FSUB:
            print(f"[FSUB] uid={uid} joined invite-mode but no pending gate -- skipping")
            return

        asyncio.create_task(_try_auto_deliver(cyber, uid))

    from pyrogram import filters as pyro_filters

    @cyber.on_callback_query(pyro_filters.regex("^fsub_done_noop$"))
    async def _fsub_noop(_, q):
        await q.answer("Already done!", show_alert=False)

    print("[FSUB] FSub handlers registered.")


# ═══════════════════════════════════════════════════════════════
# PUBLIC HELPERS (used by command.py / menus.py)
# ═══════════════════════════════════════════════════════════════

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
    m     = _m()
    ch    = getattr(m, f"FSUB_CHANNEL{slot}", None)
    lk    = getattr(m, f"FSUB_LINK{slot}",    None)
    mode  = getattr(m, f"FSUB_MODE{slot}",    "request")
    ch2   = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
    thr   = getattr(m, f"COUNT_THRESHOLD{slot}", None)
    cnt   = getattr(m, f"_req_count{slot}",   0)
    title = None
    if ch:
        try:
            title = (await bot.get_chat(ch)).title
        except Exception:
            title = "Unknown"
    db_count = await _count_reqs(slot) if (ch and mode == "request") else None
    return {
        "slot": slot, "ch": ch, "title": title, "link": lk,
        "mode": mode, "ch2": ch2, "threshold": thr,
        "counter": cnt, "db_count": db_count,
    }
