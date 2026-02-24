"""
fsub.py — SMART FSUB v3

NEW FEATURES:
  1. Already-joined members never see the join button for channels they're in.
     Each slot button is shown only if the user hasn't joined that slot yet.

  2. When a user clicks a "Request to Join" link, the join_request event
     saves them to DB AND immediately removes that slot's button from the
     message, replacing it with ✅ Requested.

  3. When ALL slots are satisfied (joined or requested), the keyboard
     automatically replaces itself with just the RETRY DOWNLOAD button —
     no manual retry needed.

  4. Auto-retry: after the join_request event fires and all slots are done,
     the bot silently delivers the files automatically (zero clicks needed).

HOW IT WORKS:
  - build_fsub_keyboard() now takes per_slot_status dict so it can
    hide buttons for already-joined slots.
  - get_slot_status(cyber, uid) returns per-slot join state for a user.
  - On join_request event: update DB, then call _try_auto_deliver() which
    checks if all slots are now satisfied and delivers files if so.
  - command.py passes the fsub message_id so we can edit it later.
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


_join_reqs_db = mongo_client["JoinReqs"]

# ── module globals ────────────────────────────────────────────
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

# ── pending fsub messages: uid → {msg, payload, chat_id} ─────
# Stored so join_request event can edit the message and auto-deliver
_PENDING_FSUB: dict[int, dict] = {}


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
        print(f"[FSUB] _add_req: col=None for slot={slot} ch={_get_ch(slot)}")
        return
    def _sync():
        try:
            result = col.update_one(
                {"user_id": int(user_id)},
                {"$setOnInsert": {
                    "_id": int(user_id), "user_id": int(user_id),
                    "first_name": first_name, "username": username,
                    "date": datetime.utcnow(),
                }},
                upsert=True
            )
            print(f"[FSUB] _add_req: uid={user_id} slot={slot} col={col.name} "
                  f"upserted={result.upserted_id is not None} matched={result.matched_count}")
        except Exception as e:
            print(f"[FSUB] _add_req error: {e}")
    await asyncio.to_thread(_sync)


async def _has_req(slot: int, user_id: int) -> bool:
    col = _col(slot)
    if col is None:
        return False
    def _sync():
        doc = col.find_one({"user_id": int(user_id)})
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
        print(f"[FSUB] API error: uid={user_id} ch={channel_id} err={e}")
        return False


# ── PER-SLOT STATUS ───────────────────────────────────────────
#
# Returns a list of dicts, one per configured slot:
# {
#   "slot":      1,
#   "ch":        -100123,
#   "link":      "https://t.me/+xxx",
#   "mode":      "request" | "invite",
#   "joined":    True/False,   ← IS an actual member right now (Telegram API)
#   "requested": True/False,   ← sent join request but NOT yet accepted
#                                 (DB record exists AND not a member yet)
# }
#
# ACCESS RULES (applied in check_fsub):
#   mode=request → MUST be an actual member (joined=True)
#                  "requested" only changes the UI button, not access.
#                  The admin must APPROVE the request first.
#   mode=invite  → MUST be an actual member (joined=True)

async def get_slot_statuses(cyber, user_id: int) -> list[dict]:
    m = _m()
    result = []

    slots = [
        (getattr(m, "FSUB_CHANNEL1", None), getattr(m, "FSUB_LINK1", None), getattr(m, "FSUB_MODE1", "request"), 1),
        (getattr(m, "FSUB_CHANNEL2", None), getattr(m, "FSUB_LINK2", None), getattr(m, "FSUB_MODE2", "request"), 2),
    ]

    for ch, link, mode, slot in slots:
        if not ch:
            continue

        is_member = await _is_member_via_api(cyber, ch, user_id)
        in_db     = await _has_req(slot, user_id)

        # If they're a real member and we have no DB record yet, record them
        # so future DB lookups are fast (avoids repeated API calls)
        if is_member and not in_db and mode == "request":
            await _add_req(slot, user_id)
            in_db = True

        joined    = is_member                   # actually in the channel
        requested = in_db and not is_member     # sent request, not approved yet

        result.append({
            "slot":      slot,
            "ch":        ch,
            "link":      link,
            "mode":      mode,
            "joined":    joined,
            "requested": requested,
        })

    return result


# ── MAIN CHECK ────────────────────────────────────────────────

async def check_fsub(cyber, user_id: int) -> bool:
    """
    Returns True ONLY if the user is an actual member of ALL configured slots.

    For request-mode channels:
      - joined=True  → pass (member, approved)
      - requested=True → FAIL (sent request but not approved yet)
      - neither       → FAIL (hasn't even requested)

    For invite-mode channels:
      - joined=True  → pass
      - otherwise    → FAIL
    """
    statuses = await get_slot_statuses(cyber, user_id)

    for s in statuses:
        if s["joined"]:
            # Actual member — always passes regardless of mode
            print(f"[FSUB] ✅ uid={user_id} slot={s['slot']}: pass (member)")
            continue

        # Not a member — always fails regardless of mode
        if s["requested"]:
            print(f"[FSUB] ❌ uid={user_id} slot={s['slot']}: FAIL (requested but not approved yet)")
        else:
            print(f"[FSUB] ❌ uid={user_id} slot={s['slot']}: FAIL (not joined)")
        return False

    return True


# ── SMART KEYBOARD BUILDER ────────────────────────────────────
#
# Shows a button ONLY for slots the user hasn't joined/requested yet.
# For slots already done → shows ✅ Done (no URL, just info).
# When all done → shows only RETRY DOWNLOAD.

def build_fsub_keyboard(
    bot_username: str,
    payload: str,
    statuses: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    """
    statuses: output of get_slot_statuses(). If None, shows all buttons
    (backward compat for cases where we can't await).
    """
    m = _m()
    rows = []

    if statuses is None:
        # Fallback: show all configured slots with no status info
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
        rows.append([BTN("☘️ RETRY DOWNLOAD ☘️", url=f"https://t.me/{bot_username}?start={payload}")])
        return InlineKeyboardMarkup(rows)

    # Only truly done when every slot is an actual member
    all_done = all(s["joined"] for s in statuses)

    if all_done:
        # All channels satisfied → just show retry
        rows.append([BTN("☘️ TAP TO GET FILES ☘️", url=f"https://t.me/{bot_username}?start={payload}")])
        return InlineKeyboardMarkup(rows)

    for s in statuses:
        slot_label = f"CHANNEL {s['slot']}"

        if s["joined"]:
            # Already a member — show a disabled-style done button (callback noop)
            rows.append([BTN(f"✅ JOINED {slot_label}", callback_data="fsub_done_noop")])

        elif s["requested"]:
            # Sent join request — show pending state
            rows.append([BTN(f"⏳ REQUESTED {slot_label}", callback_data="fsub_done_noop")])

        else:
            # Not joined yet — show the action button
            link = s["link"]
            mode = s["mode"]
            if link:
                emoji  = "📢" if mode == "request" else "🔗"
                action = "REQUEST TO JOIN" if mode == "request" else "JOIN"
                rows.append([BTN(f"{emoji} {action} {slot_label} {emoji}", url=link)])

    # Only show retry if at least one slot still needs action
    # (once all are requested/joined, handled by all_done above)
    rows.append([BTN("☘️ RETRY DOWNLOAD ☘️", url=f"https://t.me/{bot_username}?start={payload}")])

    return InlineKeyboardMarkup(rows)


def fsub_text(statuses: list[dict] | None = None) -> str:
    if not statuses:
        return UI(
            "⚠️ Access restricted.\n\n"
            "Please join the required channel(s) first.\n"
            "After joining, press ☘️ RETRY DOWNLOAD ☘️"
        )

    pending = [s for s in statuses if not s["joined"] and not s["requested"]]
    requested = [s for s in statuses if s["requested"]]

    lines = ["⚠️ Access Restricted\n"]

    if pending:
        lines.append("Please join the channel(s) below to continue.")

    if requested:
        lines.append(
            "Your join request is pending admin approval. "
            "Once approved, tap RETRY DOWNLOAD to get your files."
        )

    return UI("\n".join(lines))


# ── PENDING FSUB REGISTRY ─────────────────────────────────────
# Stores the fsub gate message so we can edit it when join_request fires.

def register_pending_fsub(uid: int, msg: Message, payload: str):
    """
    Call this in command.py after sending the fsub gate message.
    If there's already a pending entry for this user (they clicked a new
    link before finishing the old one), delete the old gate message first.
    """
    old = _PENDING_FSUB.get(uid)
    if old:
        # Schedule deletion of the old gate message without blocking
        try:
            import asyncio
            asyncio.get_event_loop().create_task(old["msg"].delete())
        except Exception:
            pass

    _PENDING_FSUB[uid] = {
        "msg":     msg,
        "payload": payload,
    }


def clear_pending_fsub(uid: int):
    _PENDING_FSUB.pop(uid, None)


# ── AUTO DELIVER after join_request ──────────────────────────

async def _try_auto_deliver(cyber, uid: int, slot_triggered: int):
    """
    Called after a join_request is recorded.
    1. Edits the pending fsub message keyboard to reflect new status.
    2. If ALL slots are now satisfied → edit to "TAP TO GET FILES" only,
       then auto-deliver files silently.
    """
    pending = _PENDING_FSUB.get(uid)
    if not pending:
        return

    msg     = pending["msg"]
    payload = pending["payload"]

    # Re-evaluate all slot statuses
    statuses = await get_slot_statuses(cyber, uid)

    # All slots must be actual members — requested alone is not enough
    all_done = all(s["joined"] for s in statuses)

    bot = await cyber.get_me()

    if all_done:
        # Edit keyboard to just the delivery button
        try:
            await msg.edit_reply_markup(
                InlineKeyboardMarkup([
                    [BTN("✅ ALL DONE — TAP TO GET FILES ✅",
                         url=f"https://t.me/{bot.username}?start={payload}")]
                ])
            )
        except Exception:
            pass

        # Auto-deliver: set session and deliver
        try:
            from utils.cyber_filter import decode_payload_async
            decoded = await decode_payload_async(payload)
            if decoded:
                owner, series, season, lang, quality = decoded
                if int(owner) == uid:
                    from CyberTG.files import set_session, deliver_files_direct
                    set_session(uid, {
                        "series":   series,
                        "season":   season,
                        "language": lang,
                        "quality":  quality,
                    })
                    await deliver_files_direct(cyber, uid)
                    clear_pending_fsub(uid)
                    print(f"[FSUB] ✅ Auto-delivered to uid={uid} after all slots satisfied")
        except Exception as e:
            print(f"[FSUB] Auto-deliver error: {e}")

    else:
        # Some slots still pending — update keyboard to show new state
        new_kb = build_fsub_keyboard(bot.username, payload, statuses)
        try:
            await msg.edit_reply_markup(new_kb)
        except Exception:
            pass


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


# ── REGISTER HANDLERS ─────────────────────────────────────────

def register_fsub_handlers(cyber: Client):
    """
    Call this in main.py AFTER the Client is created but BEFORE cyber.start().
    """

    @cyber.on_chat_join_request()
    async def _on_join_request(bot, join_req: ChatJoinRequest):
        m = _m()
        try:
            chat_id    = join_req.chat.id
            uid        = join_req.from_user.id
            first_name = join_req.from_user.first_name or ""
            username   = join_req.from_user.username   or ""

            ch1 = getattr(m, "FSUB_CHANNEL1", None)
            ch2 = getattr(m, "FSUB_CHANNEL2", None)

            print(f"[FSUB] ⚡ JoinRequest: uid={uid} chat={chat_id} ch1={ch1} ch2={ch2}")

            slot_triggered = None

            if ch1 and chat_id == ch1 and getattr(m, "FSUB_MODE1", "request") == "request":
                await _add_req(1, uid, first_name, username)
                m._req_count1 += 1
                await _rotate_slot(1)
                slot_triggered = 1
                print(f"[FSUB] ✅ Recorded slot=1 uid={uid}")

            elif ch2 and chat_id == ch2 and getattr(m, "FSUB_MODE2", "request") == "request":
                await _add_req(2, uid, first_name, username)
                m._req_count2 += 1
                await _rotate_slot(2)
                slot_triggered = 2
                print(f"[FSUB] ✅ Recorded slot=2 uid={uid}")

            else:
                print(f"[FSUB] ⚠️ JoinRequest from unregistered chat {chat_id}")
                return

            # Update keyboard and auto-deliver if all slots satisfied
            asyncio.create_task(_try_auto_deliver(cyber, uid, slot_triggered))

        except Exception as e:
            print(f"[FSUB] ❌ JOIN REQUEST ERROR: {e}")

    # Noop handler for the "✅ JOINED" / "⏳ REQUESTED" buttons
    from pyrogram import filters as pyro_filters

    @cyber.on_callback_query(pyro_filters.regex("^fsub_done_noop$"))
    async def _fsub_noop(_, q):
        await q.answer("✅ Already done for this channel!", show_alert=False)

    print("[FSUB] ✅ FSub handlers registered on client instance.")


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
