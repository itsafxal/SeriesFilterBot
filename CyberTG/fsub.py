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
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, ChatJoinRequest, Message
from pyrogram.enums import ChatMemberStatus

from utils.cyber_font import BTN, UI
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
# Cleared when sender_worker finishes naturally
# Prevents double delivery from race between join_request event + /start click
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
    """
    Returns True if lock was acquired (safe to deliver).
    Returns False if already locked (delivery in progress or just done).
    """
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
# ═══════════════════════════════════════════════════════════════

async def _is_member_via_api(cyber, channel_id: int, user_id: int) -> bool:
    try:
        member = await cyber.get_chat_member(channel_id, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,
        )
    except Exception as e:
        print(f"[FSUB] API error uid={user_id} ch={channel_id}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# SLOT STATUS
#
# Returns ONE entry per CONFIGURED slot.
# If no slots configured → returns [] → all() = True → pass.
#
# Per slot:
#   joined    = True  → user is actual Telegram member right now
#   requested = True  → user sent join request (request mode only), not yet approved
#
# PASS LOGIC per slot:
#   request mode → joined OR requested → pass
#   invite  mode → joined ONLY         → pass
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
    """
    Shows join/request buttons ONLY for slots the user hasn't satisfied yet.
    Slots already done → hidden completely.
    All done → empty keyboard (gate msg will be deleted by caller).
    statuses=None → show all configured slots (initial view, status unknown).
    """
    m    = _m()
    rows = []

    if statuses is None:
        # Initial view — show all configured slots
        raw_slots = [
            (getattr(m, "FSUB_LINK1", None), getattr(m, "FSUB_MODE1", "request"), "CHANNEL 1"),
            (getattr(m, "FSUB_LINK2", None), getattr(m, "FSUB_MODE2", "request"), "CHANNEL 2"),
        ]
        for link, mode, label in raw_slots:
            if not link:
                continue
            emoji  = "📢" if mode == "request" else "🔗"
            action = "REQUEST TO JOIN" if mode == "request" else "JOIN"
            rows.append([BTN(f"{emoji} {action} {label} {emoji}", url=link)])
        return InlineKeyboardMarkup(rows)

    # Per-status view
    for s in statuses:
        if _slot_passes(s):
            continue   # already done — hide button
        link  = s["link"]
        mode  = s["mode"]
        label = f"CHANNEL {s['slot']}"
        if link:
            emoji  = "📢" if mode == "request" else "🔗"
            action = "REQUEST TO JOIN" if mode == "request" else "JOIN"
            rows.append([BTN(f"{emoji} {action} {label} {emoji}", url=link)])

    return InlineKeyboardMarkup(rows)


def fsub_text(statuses: list[dict] | None = None) -> str:
    if not statuses:
        return (
            "⚠️ <b>Access Restricted</b>\n\n"
            "Please join the required channel(s) to get your files.\n"
            "After joining, your files will be sent automatically."
        )

    pending   = [s for s in statuses if not _slot_passes(s)]
    satisfied = [s for s in statuses if _slot_passes(s)]

    lines = ["⚠️ <b>Access Restricted</b>\n"]

    if pending:
        lines.append("📢 Please join the channel(s) below to continue.")
        lines.append("Your files will be sent <b>automatically</b> after joining.")

    if satisfied:
        lines.append(f"\n✅ {len(satisfied)} channel(s) already done.")

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
# Shared by _try_auto_deliver AND command.py /start handler.
# Uses delivery lock to prevent duplicates from any source.
# ═══════════════════════════════════════════════════════════════

async def do_deliver(cyber, uid: int, payload: str, source: str = "unknown") -> bool:
    """
    Decodes payload, sets session, delivers files, sends retry button.
    Returns True if delivery was started, False if locked (already running).

    source: label for logging ("auto_deliver" | "start" | "retry")

    RETRY BEHAVIOUR:
      - First call   → acquires lock → delivers
      - Retry while running → lock held → returns False → caller ignores silently
      - Retry after done  → lock released → acquires lock again → re-delivers
        (this is correct — user genuinely missing files and clicked retry)
    """
    if not acquire_delivery_lock(uid):
        print(f"[FSUB] do_deliver uid={uid} source={source} → LOCKED (already running)")
        return False

    print(f"[FSUB] do_deliver uid={uid} source={source} → acquired lock")

    try:
        from utils.cyber_filter import decode_payload_async
        decoded = await decode_payload_async(payload)

        if not decoded:
            print(f"[FSUB] do_deliver uid={uid} → payload expired/invalid")
            release_delivery_lock(uid)
            await cyber.send_message(
                uid,
                "⚠️ This link has expired.\n"
                "Please search for the series again to get a new link."
            )
            return False

        owner, series, season, lang, quality = decoded

        if int(owner) != uid:
            release_delivery_lock(uid)
            await cyber.send_message(uid, "⚠️ This link is not for you.")
            return False

        from CyberTG.files import set_session, deliver_files_direct, is_sender_running
        from pyrogram import enums as _enums

        set_session(uid, {
            "series":   series,
            "season":   int(season),
            "language": lang,
            "quality":  quality,
        })

        await deliver_files_direct(cyber, uid)

        # sender_worker runs as asyncio task — lock released when it finishes
        # We schedule lock release after the task completes
        asyncio.create_task(_release_lock_when_done(uid))

        # Send retry button
        try:
            bot       = await cyber.get_me()
            retry_msg = await cyber.send_message(
                uid,
                "✅ <b>Files sent!</b>\n"
                "If anything is missing, tap below within 60 seconds.",
                reply_markup=InlineKeyboardMarkup([
                    [BTN("☘️ RETRY DOWNLOAD ☘️",
                         url=f"https://t.me/{bot.username}?start={payload}")]
                ]),
                parse_mode=_enums.ParseMode.HTML,
            )
            delete_message_later(cyber, uid, retry_msg.id, 60)
        except Exception as e:
            print(f"[FSUB] retry msg error: {e}")

        return True

    except Exception as e:
        print(f"[FSUB] do_deliver error uid={uid}: {e}")
        release_delivery_lock(uid)
        return False


async def _release_lock_when_done(uid: int):
    """
    Polls until the sender_worker task is done, then releases the delivery lock.
    This allows retry to re-deliver AFTER the current send is complete.
    """
    from CyberTG.files import is_sender_running
    # Wait up to 5 minutes for sender to finish
    for _ in range(300):
        await asyncio.sleep(1)
        if not is_sender_running(uid):
            break
    release_delivery_lock(uid)
    print(f"[FSUB] delivery lock released for uid={uid}")


# ═══════════════════════════════════════════════════════════════
# AUTO-DELIVER AFTER JOIN/REQUEST
# ═══════════════════════════════════════════════════════════════

async def _try_auto_deliver(cyber, uid: int):
    """
    Called when a join_request event fires (request mode) OR
    when the user is detected as a member in get_slot_statuses (invite mode).

    1. Re-evaluates ALL slot statuses.
    2. If all satisfied → delete gate msg → deliver.
    3. If some still pending → update keyboard to show only remaining slots.
    """
    pending = _PENDING_FSUB.get(uid)
    if not pending:
        # No pending gate message — nothing to do
        # (user may have clicked /start directly without going through gate)
        return

    msg     = pending["msg"]
    payload = pending["payload"]

    statuses = await get_slot_statuses(cyber, uid)
    done     = all_slots_passed(statuses)

    if done:
        # Delete gate message
        try:
            await msg.delete()
        except Exception:
            pass
        clear_pending_fsub(uid)

        # Deliver (delivery lock prevents duplicate if /start also fires)
        await do_deliver(cyber, uid, payload, source="auto_deliver")

    else:
        # Update keyboard — show only remaining slots
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
        """
        Fires ONLY for request-mode channels when user taps the join request link.
        Invite-mode channels do NOT trigger this event — those are handled
        via get_slot_statuses() API check in /start.
        """
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

            # Try auto-deliver now that a slot has been satisfied
            asyncio.create_task(_try_auto_deliver(cyber, uid))

        except Exception as e:
            print(f"[FSUB] JOIN REQUEST ERROR: {e}")

    from pyrogram import filters as pyro_filters

    @cyber.on_callback_query(pyro_filters.regex("^fsub_done_noop$"))
    async def _fsub_noop(_, q):
        await q.answer("✅ Already done!", show_alert=False)

    print("[FSUB] ✅ FSub handlers registered.")


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
