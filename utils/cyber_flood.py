"""
utils/cyber_flood.py — CENTRALIZED FLOODWAIT MANAGER
=====================================================
Single source of truth for all Telegram API retry logic.

Features:
  1. Adaptive cooldown — base delay scales up with repeated FloodWaits
  2. Jitter — ±15% randomisation to avoid thundering-herd
  3. Foreground retry — caller-visible sleep; no fire-and-forget
  4. Global suppression — flood_cooldown() lets callers sleep before
     making *any* API call when the bot is throttled globally
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Optional, TypeVar

from pyrogram.errors import FloodWait
from config import logger

# ── Global adaptive cooldown state ────────────────────────────
# All modules share this single view of "how hot the bot is".

_cooldown_base: float = 3.0          # current base delay (seconds)
_cooldown_max:  float = 90.0         # hard ceiling
_cooldown_step_up:   float = 4.0     # added after each FloodWait
_cooldown_step_down: float = 0.3     # subtracted after each successful call
_last_flood: float = 0.0             # monotonic timestamp of last FloodWait
_success_streak: int = 0             # consecutive successes without FloodWait

# ── Public API ────────────────────────────────────────────────

async def flood_cooldown():
    """
    Sleep if the global cooldown is active.

    Call this BEFORE any Telegram API request (send_message, edit_text, etc.)
    when you are inside a loop that might be hammering the API.
    The sleep duration shrinks automatically as success_streak grows.
    """
    global _cooldown_base
    if _cooldown_base <= 2.0:
        return  # effectively not throttled
    wait = min(_cooldown_base, _cooldown_max)
    # jitter ±15%
    jitter = wait * 0.15 * (random.random() * 2 - 1)
    await asyncio.sleep(wait + jitter)


def _bump_cooldown(wait_seconds: float):
    """Increase global cooldown after a FloodWait."""
    global _cooldown_base, _last_flood, _success_streak
    _last_flood = time.monotonic()
    _success_streak = 0
    _cooldown_base = min(_cooldown_base + _cooldown_step_up, _cooldown_max)
    logger.warning(
        f"[FLOOD] FloodWait {wait_seconds:.0f}s → cooldown now {_cooldown_base:.1f}s"
    )


def _cool_down():
    """Gradually reduce global cooldown after a successful call."""
    global _cooldown_base, _success_streak
    _success_streak += 1
    if _success_streak >= 5 and _cooldown_base > 2.0:
        _cooldown_base = max(_cooldown_base - _cooldown_step_down, 2.0)


T = TypeVar("T")


async def retry_on_flood(
    coro_factory,
    *,
    max_retries: int = 5,
    label: str = "",
) -> T:
    """
    Call `coro_factory()` (an async callable that returns an awaitable).
    Automatically retry on FloodWait with the globally-tracked cooldown.

    Parameters
    ----------
    coro_factory : async callable
        Must be a factory (e.g. ``lambda: bot.send_message(...)``) so each
        retry gets a fresh coroutine.
    max_retries : int
        Maximum attempts (including the first one).  Default 5.
    label : str
        Human-readable label for log messages.

    Returns
    -------
    The result of the successful call.
    """
    for attempt in range(1, max_retries + 1):
        try:
            result = await coro_factory()
            _cool_down()
            return result
        except FloodWait as fw:
            wait = max(int(getattr(fw, "value", 10)), 5)
            _bump_cooldown(wait)
            sleep = wait + 2 + random.uniform(0, 1)
            tag = f"[{label}]" if label else ""
            logger.warning(
                f"[FLOOD]{tag} attempt {attempt}/{max_retries} — sleeping {sleep:.1f}s"
            )
            await asyncio.sleep(sleep)
        except Exception:
            # Non-FloodWait errors: still count as success for cooldown purposes
            _cool_down()
            raise
    raise RuntimeError(
        f"retry_on_flood: exhausted {max_retries} attempts due to FloodWait"
    )


# Convenience wrappers (thin, type-safe)

async def safe_send(bot, uid: int, text: str, **kw):
    """Send a message with automatic FloodWait retry."""
    from pyrogram.types import Message

    return await retry_on_flood(
        lambda: bot.send_message(uid, text, **kw),
        label="send",
    )


async def safe_edit(msg, text: Optional[str] = None, **kw):
    """Edit a message with automatic FloodWait retry."""
    from pyrogram.types import Message

    return await retry_on_flood(
        lambda: msg.edit(text, **kw) if text else msg.edit(**kw),
        label="edit",
    )


async def safe_call(coro_factory, *, label: str = "", max_retries: int = 5):
    """Generic safe wrapper — pass a callable that returns a coroutine."""
    return await retry_on_flood(coro_factory, max_retries=max_retries, label=label)


def get_cooldown_info() -> dict:
    """Return current cooldown state (for /stats debugging)."""
    return {
        "base": round(_cooldown_base, 1),
        "max": _cooldown_max,
        "last_flood_ago": round(time.monotonic() - _last_flood, 1) if _last_flood else None,
        "success_streak": _success_streak,
    }


# ── Callback answer debounce ──────────────────────────────────
# Prevents "QUERY_ID_INVALID" from rapid duplicate q.answer() calls
# and avoids hitting FloodWait on answer flood.
_last_answer: dict[str, float] = {}       # key = f"{chat_id}:{query_id}"
_ANSWER_DEBOUNCE_S: float = 0.8            # seconds between same-query answers
_MAX_ANSWER_ENTRIES: int = 2000


async def safe_answer(q, text: Optional[str] = None, *, show_alert: bool = False, cache_time: int = 0):
    """Answer a callback query with debounce — silently drops duplicates.

    Call instead of raw ``await q.answer(...)`` to prevent:
      - double-click answer storms
      - FloodWait on answer flood
      - QUERY_ID_INVALID from expired query_ids
    """
    try:
        key = f"{q.message.chat.id}:{q.id}"
        now = time.monotonic()
        if key in _last_answer and (now - _last_answer[key]) < _ANSWER_DEBOUNCE_S:
            return  # already answered recently — skip
        _last_answer[key] = now
        # Cap dict size
        if len(_last_answer) > _MAX_ANSWER_ENTRIES:
            try:
                _last_answer.pop(next(iter(_last_answer)))
            except StopIteration:
                pass
        await q.answer(text, show_alert=show_alert, cache_time=cache_time)
    except Exception:
        pass  # query too old or FloodWait — best-effort


def _prune_answer_cache():
    """Remove stale entries older than 30 s (call periodically)."""
    now = time.monotonic()
    stale = [k for k, v in _last_answer.items() if now - v > 30]
    for k in stale:
        _last_answer.pop(k, None)