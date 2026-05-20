from __future__ import annotations
import asyncio
import time
from datetime import datetime
from pyrogram import Client, filters as pyro_filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message,
    ChatJoinRequest, ChatMemberUpdated,
)
from pyrogram.enums import ChatMemberStatus
from DB.database import get_global_setting, set_global_setting, mongo_client
from utils.cyber_flood import retry_on_flood, safe_answer
from config import logger, bot_username, bot_id

_join_reqs_db = mongo_client["JoinReqs"]

FSUB_CHANNEL1: int | None = None
FSUB_LINK1: str | None = None
FSUB_MODE1: str = "request"
FSUB_CHANNEL1_2: int | None = None
FSUB_LINK1_2: str | None = None

FSUB_CHANNEL2: int | None = None
FSUB_LINK2: str | None = None
FSUB_MODE2: str = "request"
FSUB_CHANNEL2_2: int | None = None
FSUB_LINK2_2: str | None = None

COUNT_THRESHOLD1: int | None = None
COUNT_THRESHOLD2: int | None = None
_req_count1: int = 0
_req_count2: int = 0

_PENDING_FSUB: dict[int, dict] = {}
_MAX_PENDING_FSUB = 500
_DELIVERY_LOCK: dict[int, float] = {}
_LOCK_TTL = 300

# ---------------------------------------------------------------------------
# Membership check cache — avoids redundant get_chat_member API calls
# ---------------------------------------------------------------------------
_member_cache: dict[tuple, tuple[bool, float]] = {}
_MEMBER_CACHE_TTL = 30.0  # seconds

def _m():
    import sys
    return sys.modules[__name__]

def _col(slot: int):
    cid = getattr(_m(), f"FSUB_CHANNEL{slot}", None)
    return _join_reqs_db[str(cid)] if cid else None

# ----------------------------------------------------------------------
# Delivery lock with TTL (prevents ghost locks)
# ----------------------------------------------------------------------
def acquire_delivery_lock(uid: int) -> bool:
    now = time.monotonic()
    existing = _DELIVERY_LOCK.get(uid)
    if existing is not None and now - existing < _LOCK_TTL:
        return False
    if len(_DELIVERY_LOCK) > 500:
        cutoff = now - _LOCK_TTL
        stale = [u for u, ts in list(_DELIVERY_LOCK.items()) if ts < cutoff]
        for u in stale:
            _DELIVERY_LOCK.pop(u, None)
    _DELIVERY_LOCK[uid] = now
    return True

def release_delivery_lock(uid: int):
    _DELIVERY_LOCK.pop(uid, None)

# ----------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------
async def _add_req(slot: int, user_id: int, first_name: str = "", username: str = ""):
    col = _col(slot)
    if col is None:
        return
    def _sync():
        col.update_one(
            {"user_id": int(user_id)},
            {"$setOnInsert": {
                "_id": int(user_id),
                "user_id": int(user_id),
                "first_name": first_name,
                "username": username,
                "date": datetime.utcnow(),
            }},
            upsert=True
        )
    await asyncio.to_thread(_sync)

async def _has_req(slot: int, user_id: int) -> bool:
    col = _col(slot)
    if col is None:
        return False
    return await asyncio.to_thread(lambda: col.find_one({"user_id": int(user_id)}) is not None)

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

# ----------------------------------------------------------------------
# Load / save settings
# ----------------------------------------------------------------------
DB_KEY = "fsub_config_v2"

async def load_fsub_settings():
    global FSUB_CHANNEL1, FSUB_LINK1, FSUB_MODE1, FSUB_CHANNEL1_2, FSUB_LINK1_2
    global FSUB_CHANNEL2, FSUB_LINK2, FSUB_MODE2, FSUB_CHANNEL2_2, FSUB_LINK2_2
    global COUNT_THRESHOLD1, COUNT_THRESHOLD2, _req_count1, _req_count2
    try:
        d = await get_global_setting(DB_KEY) or {}
    except Exception:
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

async def save_fsub_settings():
    m = _m()
    data = {
        "ch1":   getattr(m, "FSUB_CHANNEL1", None),
        "lk1":   getattr(m, "FSUB_LINK1", None),
        "mode1": getattr(m, "FSUB_MODE1", "request"),
        "ch1_2": getattr(m, "FSUB_CHANNEL1_2", None),
        "lk1_2": getattr(m, "FSUB_LINK1_2", None),
        "ch2":   getattr(m, "FSUB_CHANNEL2", None),
        "lk2":   getattr(m, "FSUB_LINK2", None),
        "mode2": getattr(m, "FSUB_MODE2", "request"),
        "ch2_2": getattr(m, "FSUB_CHANNEL2_2", None),
        "lk2_2": getattr(m, "FSUB_LINK2_2", None),
        "thr1":  getattr(m, "COUNT_THRESHOLD1", None),
        "thr2":  getattr(m, "COUNT_THRESHOLD2", None),
        "cnt1":  getattr(m, "_req_count1", 0),
        "cnt2":  getattr(m, "_req_count2", 0),
    }
    await set_global_setting(DB_KEY, data)

# ----------------------------------------------------------------------
# Join state checks
# ----------------------------------------------------------------------
async def _is_member_via_api(cyber, channel_id: int, user_id: int) -> bool:
    """Check membership with automatic FloodWait retry (max 2 attempts)."""
    key = (channel_id, user_id)
    now = time.monotonic()
    cached = _member_cache.get(key)
    if cached is not None:
        val, ts = cached
        if now - ts < _MEMBER_CACHE_TTL:
            return val
    try:
        member = await retry_on_flood(
            lambda: cyber.get_chat_member(channel_id, user_id),
            label="fsub.check", max_retries=2,
        )
        result = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except Exception:
        result = False
    # Prune stale entries when cache grows too large
    if len(_member_cache) > 2000:
        stale = [k for k, (_, ts) in _member_cache.items() if now - ts > _MEMBER_CACHE_TTL]
        for k in stale:
            _member_cache.pop(k, None)
    _member_cache[key] = (result, now)
    return result

async def get_slot_statuses(cyber, user_id: int) -> list[dict]:
    m = _m()
    result = []
    for slot in (1, 2):
        ch = getattr(m, f"FSUB_CHANNEL{slot}", None)
        if not ch:
            continue
        mode = getattr(m, f"FSUB_MODE{slot}", "request")
        link = getattr(m, f"FSUB_LINK{slot}", None)
        is_member = await _is_member_via_api(cyber, ch, user_id)
        if mode == "request":
            in_db = await _has_req(slot, user_id)
            if is_member and not in_db:
                await _add_req(slot, user_id)
                in_db = True
            joined = is_member
            requested = in_db and not is_member
        else:
            joined = is_member
            requested = False
        result.append({
            "slot": slot,
            "ch": ch,
            "link": link,
            "mode": mode,
            "joined": joined,
            "requested": requested,
        })
    return result

def all_slots_passed(statuses: list[dict]) -> bool:
    return all(s["joined"] or s["requested"] for s in statuses) if statuses else True

# ----------------------------------------------------------------------
# FSub UI
# ----------------------------------------------------------------------
def build_fsub_keyboard(
    bot_username: str,
    payload: str,
    statuses: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    m = _m()
    rows = []
    if statuses:
        for s in statuses:
            if s["joined"] or s["requested"]:
                continue
            label = "Request to Join" if s["mode"] == "request" else "Join Channel"
            rows.append([InlineKeyboardButton(f"📢 Channel {s['slot']} — {label}", url=s["link"])])
    else:
        for slot in (1, 2):
            link = getattr(m, f"FSUB_LINK{slot}", None)
            mode = getattr(m, f"FSUB_MODE{slot}", "request")
            if link:
                label = "Request to Join" if mode == "request" else "Join Channel"
                rows.append([InlineKeyboardButton(f"📢 Channel {slot} — {label}", url=link)])
    return InlineKeyboardMarkup(rows)

def fsub_text(statuses: list[dict] | None = None) -> str:
    if not statuses:
        return "🔒 <b>Members Only</b>\n\nJoin the required channel(s) below to access your files."
    pending = [s for s in statuses if not (s["joined"] or s["requested"])]
    satisfied = [s for s in statuses if s["joined"] or s["requested"]]
    lines = [
        "🔒 <b>Members Only</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    if pending:
        count = len(pending)
        lines.append(f"Join <b>{count}</b> more channel{'s' if count > 1 else ''} to unlock your files.")
        lines.append("<i>Files will be sent automatically once you're done.</i>")
    if satisfied:
        lines.append("")
        lines.append(f"✅  <b>{len(satisfied)}</b> channel{'s' if len(satisfied) > 1 else ''} completed.")
    return "\n".join(lines)

def register_pending_fsub(uid: int, msg: Message, payload: str, via_filebot: bool = False):
    # Cap pending entries — evict oldest on overflow
    if len(_PENDING_FSUB) >= _MAX_PENDING_FSUB and uid not in _PENDING_FSUB:
        oldest_uid = next(iter(_PENDING_FSUB))
        try:
            asyncio.create_task(_PENDING_FSUB[oldest_uid]["msg"].delete())
        except Exception:
            pass
        _PENDING_FSUB.pop(oldest_uid, None)
    old = _PENDING_FSUB.get(uid)
    if old:
        try:
            asyncio.create_task(old["msg"].delete())
        except Exception:
            pass
    _PENDING_FSUB[uid] = {"msg": msg, "payload": payload, "via_filebot": via_filebot}

def clear_pending_fsub(uid: int):
    _PENDING_FSUB.pop(uid, None)

async def do_deliver(cyber, uid: int, payload: str, source: str = "unknown") -> bool:
    if not acquire_delivery_lock(uid):
        return False
    try:
        from utils.cyber_filter import decode_payload_async
        decoded = await decode_payload_async(payload)
        if not decoded:
            release_delivery_lock(uid)
            await cyber.send_message(uid, "⏳ <b>Link Expired</b>\n\nPlease search again.", parse_mode="html")
            return False
        owner, series, season, lang, quality = decoded
        if int(owner) != uid:
            release_delivery_lock(uid)
            await cyber.send_message(uid, "⛔ <b>Not Your Link</b>\n\nThis link belongs to another user.", parse_mode="html")
            return False
        from CyberTG.files import set_session, deliver_files_direct
        set_session(uid, {
            "series": series,
            "season": int(season),
            "language": lang,
            "quality": quality,
        })
        async def _after():
            release_delivery_lock(uid)
        await deliver_files_direct(cyber, uid, on_complete=_after)
        return True
    except Exception as e:
        logger.error(f"[FSUB] do_deliver error: {e}")
        release_delivery_lock(uid)
        return False

async def _try_auto_deliver(cyber, uid: int):
    pending = _PENDING_FSUB.get(uid)
    if not pending:
        return
    msg = pending["msg"]
    payload = pending["payload"]
    statuses = await get_slot_statuses(cyber, uid)
    if all_slots_passed(statuses):
        try:
            await msg.delete()
        except Exception:
            pass
        clear_pending_fsub(uid)
        await do_deliver(cyber, uid, payload, source="auto_deliver")
    else:
        new_kb = build_fsub_keyboard(bot_username or "", payload, statuses)
        try:
            await msg.edit_reply_markup(new_kb)
        except Exception:
            pass

async def _rotate_slot(slot: int):
    m = _m()
    thr = getattr(m, f"COUNT_THRESHOLD{slot}", None)
    cnt = getattr(m, f"_req_count{slot}", 0)
    if not thr or cnt <= thr:
        return
    ch2 = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
    lk2 = getattr(m, f"FSUB_LINK{slot}_2", None)
    if ch2:
        setattr(m, f"FSUB_CHANNEL{slot}", ch2)
        setattr(m, f"FSUB_LINK{slot}", lk2)
        setattr(m, f"FSUB_CHANNEL{slot}_2", None)
        setattr(m, f"FSUB_LINK{slot}_2", None)
    else:
        logger.warning(
            f"[FSUB] Slot {slot} rotated with NO secondary channel — slot disabled! "
            f"Threshold {thr} reached with {cnt} requests."
        )
        setattr(m, f"FSUB_CHANNEL{slot}", None)
        setattr(m, f"FSUB_LINK{slot}", None)
    setattr(m, f"_req_count{slot}", 0)
    await _clear_reqs(slot)
    await save_fsub_settings()

# ----------------------------------------------------------------------
# PUBLIC HELPER FUNCTIONS (used by menus.py)
# ----------------------------------------------------------------------
async def set_fsub_slot(slot: int, channel_id: int, link: str, mode: str, secondary: bool = False):
    m = _m()
    if secondary:
        setattr(m, f"FSUB_CHANNEL{slot}_2", channel_id)
        setattr(m, f"FSUB_LINK{slot}_2", link)
    else:
        setattr(m, f"FSUB_CHANNEL{slot}", channel_id)
        setattr(m, f"FSUB_LINK{slot}", link)
        setattr(m, f"FSUB_MODE{slot}", mode)
        setattr(m, f"_req_count{slot}", 0)
    await save_fsub_settings()

async def del_fsub_slot(slot: int, secondary: bool = False) -> int | None:
    m = _m()
    if secondary:
        old = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
        setattr(m, f"FSUB_CHANNEL{slot}_2", None)
        setattr(m, f"FSUB_LINK{slot}_2", None)
    else:
        old = getattr(m, f"FSUB_CHANNEL{slot}", None)
        setattr(m, f"FSUB_CHANNEL{slot}", None)
        setattr(m, f"FSUB_LINK{slot}", None)
        setattr(m, f"_req_count{slot}", 0)
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
    ch = getattr(m, f"FSUB_CHANNEL{slot}", None)
    lk = getattr(m, f"FSUB_LINK{slot}", None)
    mode = getattr(m, f"FSUB_MODE{slot}", "request")
    ch2 = getattr(m, f"FSUB_CHANNEL{slot}_2", None)
    thr = getattr(m, f"COUNT_THRESHOLD{slot}", None)
    cnt = getattr(m, f"_req_count{slot}", 0)
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

# ----------------------------------------------------------------------
# Register event handlers
# ----------------------------------------------------------------------
def register_fsub_handlers(cyber: Client):
    @cyber.on_chat_join_request()
    async def _on_join_request(bot, join_req: ChatJoinRequest):
        m = _m()
        chat_id = join_req.chat.id
        uid = join_req.from_user.id
        first_name = join_req.from_user.first_name or ""
        username = join_req.from_user.username or ""
        ch1 = getattr(m, "FSUB_CHANNEL1", None)
        ch2 = getattr(m, "FSUB_CHANNEL2", None)
        try:
            if ch1 and chat_id == ch1 and getattr(m, "FSUB_MODE1", "request") == "request":
                await _add_req(1, uid, first_name, username)
                m._req_count1 += 1
                await _rotate_slot(1)
            elif ch2 and chat_id == ch2 and getattr(m, "FSUB_MODE2", "request") == "request":
                await _add_req(2, uid, first_name, username)
                m._req_count2 += 1
                await _rotate_slot(2)
            else:
                return
            asyncio.create_task(_try_auto_deliver(cyber, uid))
        except Exception as e:
            logger.error(f"[FSUB] JOIN REQUEST ERROR: {e}")

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
            uid = new_status.user.id if new_status.user else None
            chat_id = update.chat.id if update.chat else None
        except Exception:
            return
        if not uid or not chat_id:
            return
        if uid == bot_id:
            return
        ch1 = getattr(m, "FSUB_CHANNEL1", None)
        mode1 = getattr(m, "FSUB_MODE1", "request")
        ch2 = getattr(m, "FSUB_CHANNEL2", None)
        mode2 = getattr(m, "FSUB_MODE2", "request")
        matched = False
        if ch1 and chat_id == ch1 and mode1 == "invite":
            matched = True
        elif ch2 and chat_id == ch2 and mode2 == "invite":
            matched = True
        if not matched:
            return
        if uid not in _PENDING_FSUB:
            return
        asyncio.create_task(_try_auto_deliver(cyber, uid))

    @cyber.on_callback_query(pyro_filters.regex("^fsub_done_noop$"))
    async def _fsub_noop(_, q):
        await safe_answer(q, "Already done!", show_alert=False)
