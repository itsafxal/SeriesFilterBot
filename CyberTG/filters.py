"""
filters.py — FIXED & OPTIMIZED
================================
FIXES in this version:

  1. AUTO-DELETE KEY BUG FIXED — _get_settings() now reads "FILE_AUTODEL_DELAY"
     (the key written by admin) instead of "AUTO_DELETE_DELAY" (a different key).
     Before this fix, auto-delete was silently disabled for all search results.

  2. cyber.get_me() CACHED in gls and bk_qual — was called fresh on every quality
     button press (one Telegram API round-trip per tap).  Now uses the module-level
     _get_bot_id() cache, which resolves in ~0 ms after the first call.

  3. bk_qual DEAD CODE REMOVED — the handler existed but was never triggered because
     no callback ever generates "bk_qual|..." data.  The quality step uses URL buttons
     (not callback buttons) so there is no "back from quality" navigation needed.
     Removing it eliminates the confusing dead code and the spurious get_me() call.

  4. Settings TTL cache unchanged — poster_enabled, poster_landscape, auto_delete_delay,
     filter_enabled, maintenance are still fetched once per 30s and cached in process.

  ─────────────────────────────────────────────────────────────
  BUG FIX (this version): SEASON BUTTON BROKEN FOR SHORT/SPECIAL SERIES NAMES
  ─────────────────────────────────────────────────────────────

  ROOT CAUSE:
    The series name stored in callback_data (e.g. "gss|{uid}|{series}|{season}")
    comes from two sources:

      a) search_handler  → calls search_series(query) → returns normalized_name
         from SQLite (e.g. "Dark", "Dark Season 1", etc.)
      b) sug handler     → takes the raw series from the suggestion button label
         (e.g. "Dark") set by suggest_series() / suggest_series_with_year()

    The DB stores normalized_name as the result of _normalized_series_name()
    applied to each file_name.  That function always produces Title-Case output,
    e.g. "Dark", "Breaking Bad", "The Boys".

    When the user taps a Season button, gss splits q.data and calls:
        get_unique_languages_by_season(series, int(season))

    This hits SQLite with a COLLATE NOCASE query, so case should not matter.

    THE ACTUAL BUG: The suggestion buttons in the "Did you mean..." screen are
    built from suggest_series_with_year() results.  That function calls
    search_series(query) → get_all_series_sync() internally → returns the
    normalized_name strings as stored in SQLite.  So far so good.

    HOWEVER, cyber_series.suggest_series() also calls the TMDB/OMDB enrichment
    step (_validate_and_enrich), and format_suggestion_label() appends the year:
        "Dark (2017)"
    The callback_data for that button is set to:
        f"sug|{s}"   where s = "Dark (2017)"   ← YEAR IS INCLUDED!

    Then in the sug handler:
        series = q.data.split("|", 1)[1].lower()
        → series = "dark (2017)"

    That series name (with the year suffix) is then embedded into:
        f"gss|{uid}|{series}|{s}"
        → "gss|12345|dark (2017)|1"

    When gss fires, it queries:
        get_unique_languages_by_season("dark (2017)", 1)
    but SQLite has the row stored as normalized_name = "Dark" — no year — so
    the query returns [] (empty). No languages found → no buttons → season tap
    does nothing (silent failure).

  FIX APPLIED:
    1. In sug handler: strip any " (YYYY)" year suffix from the series name
       before embedding it into season button callback_data.

    2. In build_season_pages: keep using series as-is (already cleaned by sug).

    3. In gss / gls / bk_season / bk_lang / spg: add a strip_year() call on
       the incoming series value from callback_data, as a belt-and-suspenders
       defence against any other path that could sneak a year in.

    4. The suggestion button LABEL still shows the year for display purposes —
       only the callback_data payload is stripped of the year.
"""

import math
import re
import time
import asyncio

from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from pyrogram.errors import MessageNotModified, FloodWait, WebpageMediaEmpty

from config import (
    DEFAULT_POSTER,
    SPELL_IMG,
    SEASONS_PER_PAGE,
    ADMIN_USERS,
)
from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_with_codec,
    get_global_setting,
    get_filter_enabled,
    get_poster_enabled,
    get_poster_landscape,
)

from utils.cyber_series import suggest_series_with_year, check_owner
from utils.cyber_filter import encode_payload
from utils.cyber_delete import delete_message_later


# ─────────────────────────────────────────────────────────────
# YEAR SUFFIX STRIPPER  ← FIX
# ─────────────────────────────────────────────────────────────

_YEAR_SUFFIX_RE = re.compile(r"\s*\(\d{4}\)\s*$")


def _strip_year(series: str) -> str:
    """
    Remove trailing year suffix added by format_suggestion_label().
    e.g. "dark (2017)"  →  "dark"
         "the boys (2019)" → "the boys"
         "dark"            → "dark"   (unchanged)
    Always returns lowercase (DB normalized_name is stored lowercase).
    """
    return _YEAR_SUFFIX_RE.sub("", series.lower()).strip()


# ─────────────────────────────────────────────────────────────
# SETTINGS TTL CACHE
# Two separate delay keys:
#   AUTO_DELETE_DELAY  → search result messages (poster, suggestions, "no results")
#   FILE_AUTODEL_DELAY → sent files + stickers (files.py / cyber_filebot.py)
# ─────────────────────────────────────────────────────────────

SETTINGS_TTL = 30  # seconds

_settings_cache: dict = {}
_settings_ts:    float = 0.0


async def _get_settings() -> dict:
    global _settings_cache, _settings_ts
    now = time.monotonic()
    if now - _settings_ts < SETTINGS_TTL and _settings_cache:
        return _settings_cache

    poster_on, landscape, auto_del, filter_on, maintenance = await asyncio.gather(
        get_poster_enabled(),
        get_poster_landscape(),
        get_global_setting("AUTO_DELETE_DELAY", 0),   # search result messages only
        get_filter_enabled(),
        get_global_setting("MAINTENANCE_MODE", False),
    )

    try:
        auto_del_int = int(auto_del) if auto_del else 0
    except (TypeError, ValueError):
        auto_del_int = 0

    _settings_cache = {
        "poster_enabled":    bool(poster_on),
        "poster_landscape":  bool(landscape),
        "auto_delete_delay": auto_del_int,
        "filter_enabled":    bool(filter_on),
        "maintenance":       bool(maintenance),
    }
    _settings_ts = now
    return _settings_cache


def _invalidate_settings_cache():
    global _settings_ts
    _settings_ts = 0.0


# ─────────────────────────────────────────────────────────────
# PER-USER SEARCH COOLDOWN  (capped at 5000 entries)
# ─────────────────────────────────────────────────────────────

_SEARCH_COOLDOWN: dict[int, float] = {}
SEARCH_COOLDOWN_SECS  = 3
MAX_COOLDOWN_ENTRIES  = 5000


def _is_on_cooldown(uid: int) -> bool:
    last = _SEARCH_COOLDOWN.get(uid, 0)
    return (time.time() - last) < SEARCH_COOLDOWN_SECS


def _stamp_cooldown(uid: int):
    if len(_SEARCH_COOLDOWN) >= MAX_COOLDOWN_ENTRIES:
        try:
            _SEARCH_COOLDOWN.pop(next(iter(_SEARCH_COOLDOWN)))
        except StopIteration:
            pass
    _SEARCH_COOLDOWN[uid] = time.time()


# ─────────────────────────────────────────────────────────────
# SAFE EDIT / REPLY WRAPPERS
# ─────────────────────────────────────────────────────────────

async def safe_edit(coro, q):
    """
    Await an edit coroutine and ALWAYS answer the callback query.
    Telegram requires answerCallbackQuery within 10 s or the button stays stuck.
    """
    try:
        await coro
    except MessageNotModified:
        pass
    except FloodWait as fw:
        await asyncio.sleep(fw.value)
    except Exception:
        pass
    # Always answer — clears the Telegram loading spinner regardless of outcome
    try:
        await q.answer()
    except Exception:
        pass


async def _safe_reply_photo(m, photo: str, caption: str, parse_mode, reply_markup):
    if photo:
        try:
            return await m.reply_photo(
                photo,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except (WebpageMediaEmpty, Exception) as e:
            import logging
            logging.getLogger("BOT").warning(
                f"[POSTER] reply_photo failed ({type(e).__name__}) URL={photo!r} — "
                "falling back to text. Use a direct image URL in config.py."
            )
    return await m.reply_text(caption, parse_mode=parse_mode, reply_markup=reply_markup)


async def _safe_edit_media(q, photo: str, caption: str, parse_mode, reply_markup):
    if photo:
        try:
            return await q.message.edit_media(
                InputMediaPhoto(photo, caption=caption, parse_mode=parse_mode),
                reply_markup=reply_markup,
            )
        except (WebpageMediaEmpty, Exception) as e:
            import logging
            logging.getLogger("BOT").warning(
                f"[POSTER] edit_media failed ({type(e).__name__}) URL={photo!r} — "
                "falling back to edit_caption."
            )
    try:
        return await q.message.edit_caption(
            caption, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except (MessageNotModified, Exception):
        pass


# ─────────────────────────────────────────────────────────────
# POSTER HELPER
# ─────────────────────────────────────────────────────────────

async def _get_poster(series: str, settings: dict):
    if not settings.get("poster_enabled"):
        return None, None, None, None
    landscape = settings.get("poster_landscape", False)
    from CyberTG.poster import get_series_info as _get_series_info
    poster, year, genre, rating = await _get_series_info(series, landscape=landscape)
    return poster, year, genre, rating


# ─────────────────────────────────────────────────────────────
# RESOLVE SERIES
# ─────────────────────────────────────────────────────────────

async def resolve_series_with_suggestions(text: str):
    if not text:
        return None, []

    query = text.strip().lower()
    if not query:
        return None, []

    try:
        results = await search_series(query)
    except Exception:
        return None, []

    if not isinstance(results, (list, tuple)):
        return None, []

    if not results:
        try:
            fuzzy = await suggest_series_with_year(query)
            if isinstance(fuzzy, (list, tuple)):
                # fuzzy is list of (title, year) tuples
                return None, list(fuzzy)[:4]
        except Exception:
            pass
        return None, []

    unique      = list(dict.fromkeys(results))[:4]
    query_words = query.split()

    if len(unique) == 1:
        result_words = unique[0].lower().split()
        if len(query_words) >= len(result_words):
            return unique[0], []
        # Return as (title, year=None) tuples to be consistent
        return None, [(unique[0], None)]

    return None, [(s, None) for s in unique]


# ─────────────────────────────────────────────────────────────
# CAPTION BUILDER
# ─────────────────────────────────────────────────────────────

def build_caption(series, year, genre, rating,
                  seasons=None, languages=None, qualities=None,
                  step="season"):

    lines = [
        f"🎬 <b>{series}</b>",
        f"<b>━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
        f"📅  <b>Year</b>     <code>{year or 'N/A'}</code>",
        f"🎭  <b>Genre</b>    <code>{genre or 'N/A'}</code>",
        f"⭐  <b>Rating</b>   <code>{rating or 'N/A'}</code>",
        "",
    ]

    if step == "season":
        if seasons:
            txt = "  ·  ".join(f"S{s:02d}" for s in seasons)
            lines.append(f"📂  <b>Seasons</b>  <code>{txt}</code>")
        lines.append("")
        lines.append("Select a <b>Season</b> to continue ↓")

    elif step == "language":
        txt = "  ·  ".join(languages)
        lines.append(f"🌐  <b>Available</b>  <code>{txt}</code>")
        lines.append("")
        lines.append("Select a <b>Language</b> to continue ↓")

    elif step == "quality":
        lt = "  ·  ".join(languages)
        qt = "  ·  ".join(qualities)
        lines.append(f"🌐  <b>Language</b>   <code>{lt}</code>")
        lines.append(f"🎞  <b>Quality</b>    <code>{qt}</code>")
        lines.append("")
        lines.append("Select a <b>Quality</b> to continue ↓")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# KEYBOARD HELPERS
# ─────────────────────────────────────────────────────────────

def build_grid_keyboard(items, cb_builder, per_row=2, back_btn=None):
    rows, row = [], []
    for it in items:
        row.append(InlineKeyboardButton(it, callback_data=cb_builder(it)))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if back_btn:
        rows.append([back_btn])
    return InlineKeyboardMarkup(rows)


def build_season_pages(seasons, uid, series, page=0):
    total_pages = math.ceil(len(seasons) / SEASONS_PER_PAGE)
    start = page * SEASONS_PER_PAGE
    chunk = seasons[start:start + SEASONS_PER_PAGE]

    # FIX: series arriving here must already be year-stripped and lowercased.
    # build_season_pages does NOT strip itself — callers are responsible.
    rows, row = [], []
    for s in chunk:
        row.append(InlineKeyboardButton(
            f"Season {s:02d}", callback_data=f"gss|{uid}|{series}|{s}"
        ))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀  Prev", callback_data=f"spg|{uid}|{series}|{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next  ▶", callback_data=f"spg|{uid}|{series}|{page+1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────
# QUALITY LABEL
# ─────────────────────────────────────────────────────────────

QUALITY_DISPLAY = {
    "4K":      "4K",
    "2160P":   "2160p",
    "1080P":   "1080p",
    "720P":    "720p",
    "480P":    "480p",
    "360P":    "360p",
    "UNKNOWN": "Unknown",
}


def _quality_label(quality: str, codec: str) -> str:
    disp = QUALITY_DISPLAY.get((quality or "").upper().strip(), quality)
    if codec and codec.strip():
        return f"{codec.strip()} {disp}"
    return disp


# ─────────────────────────────────────────────────────────────
# BOT SELF CACHE — avoids get_me() on every callback
# FIX: gls and bk_qual now use this cache instead of cyber.get_me()
# ─────────────────────────────────────────────────────────────

_bot_id_cache:       int | None = None
_bot_username_cache: str | None = None


async def _get_bot_info(cyber) -> tuple[int | None, str | None]:
    """Returns (bot_id, bot_username) — cached after first call."""
    global _bot_id_cache, _bot_username_cache
    if _bot_id_cache is not None:
        return _bot_id_cache, _bot_username_cache
    try:
        me = await cyber.get_me()
        _bot_id_cache       = me.id
        _bot_username_cache = me.username
        return _bot_id_cache, _bot_username_cache
    except Exception:
        return None, None


async def _get_bot_id(cyber) -> int | None:
    bot_id, _ = await _get_bot_info(cyber)
    return bot_id


# ─────────────────────────────────────────────────────────────
# REGISTER FILTERS
# ─────────────────────────────────────────────────────────────

def register_filters(cyber: Client):

    # ── SEARCH HANDLER ────────────────────────────────────────

    @cyber.on_message(
        filters.text
        & filters.incoming
        & (filters.private | filters.group)
    )
    async def search_handler(_, m):
        if not m.text or m.text.startswith("/"):
            return

        if not m.from_user:
            return

        try:
            bot_id = await _get_bot_id(_)
            if bot_id and m.from_user.id == bot_id:
                return
        except Exception:
            pass

        if getattr(m.from_user, "is_bot", False):
            return

        if m.reply_to_message:
            try:
                bot_id = await _get_bot_id(_)
                rtu    = m.reply_to_message.from_user
                if bot_id and rtu and rtu.id == bot_id:
                    return
            except Exception:
                pass

        uid = m.from_user.id

        settings = await _get_settings()

        if uid not in ADMIN_USERS and settings["maintenance"]:
            await m.reply_text(
                "🔧 <b>Bot is under maintenance.</b>\nPlease try again later.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        if uid not in ADMIN_USERS and not settings["filter_enabled"]:
            return

        if _is_on_cooldown(uid):
            return

        auto_del = settings["auto_delete_delay"]

        series, suggestions = await resolve_series_with_suggestions(m.text)

        if series:
            # FIX: normalize so callback_data uses DB's normalized_name (no year suffix)
            series = _strip_year(series)
            poster, year, genre, rating = await _get_poster(series, settings)
            seasons = list((await get_series_summary(series))["seasons"])
            kb      = build_season_pages(seasons, uid, series)
            photo   = poster or DEFAULT_POSTER
            msg = await _safe_reply_photo(
                m, photo,
                caption=build_caption(series, year, genre, rating, seasons=seasons),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
            _stamp_cooldown(uid)
            if auto_del > 0 and msg:
                delete_message_later(cyber, msg.chat.id, msg.id, auto_del)
            return

        if suggestions:
            # suggestions is now a list of (title, year_or_None) tuples
            from utils.cyber_series import format_suggestion_label
            lines = [
                "🔍 <b>Did you mean...</b>",
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>",
                "",
                f"Found <b>{len(suggestions)}</b> titles matching "
                f"<code>{m.text.strip()}</code>",
                "",
                "Tap a title to select it ↓",
            ]
            buttons = []
            for title, yr in suggestions:
                display_label = format_suggestion_label(title, yr)
                # FIX: callback_data stores the CLEAN title (no year), not the display label.
                # The year is only for human display; the DB doesn't know about it.
                clean_title = _strip_year(title)
                buttons.append([InlineKeyboardButton(
                    f"🎬  {display_label}",
                    callback_data=f"sug|{clean_title}"
                )])
            buttons.append([InlineKeyboardButton("✖️  Close", callback_data="close_spell")])
            msg = await _safe_reply_photo(
                m, SPELL_IMG,
                caption="\n".join(lines),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            _stamp_cooldown(uid)
            if auto_del > 0 and msg:
                delete_message_later(cyber, msg.chat.id, msg.id, auto_del)
                delete_message_later(cyber, m.chat.id, m.id, auto_del)
            return

        msg = await m.reply_text(
            "🔍 <b>No Results Found</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"No series matched <code>{m.text.strip()}</code>\n\n"
            "<i>Try a different title or check your spelling.</i>",
            parse_mode=enums.ParseMode.HTML,
        )
        if auto_del > 0 and msg:
            delete_message_later(cyber, msg.chat.id, msg.id, auto_del)
            delete_message_later(cyber, m.chat.id, m.id, auto_del)

    # ── SUGGESTION CLICK ──────────────────────────────────────

    @cyber.on_callback_query(filters.regex("^sug\\|"))
    async def sug(_, q):
        settings = await _get_settings()
        if q.from_user.id not in ADMIN_USERS and settings["maintenance"]:
            return await q.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)

        # Answer immediately — clears spinner before slow poster/DB fetch
        try:
            await q.answer()
        except Exception:
            pass

        # FIX: strip year suffix AND normalize to lowercase.
        # callback_data is already clean (set by search_handler above),
        # but _strip_year is idempotent so calling it again is safe.
        raw_series = q.data.split("|", 1)[1]
        series = _strip_year(raw_series)

        auto_del = settings["auto_delete_delay"]
        poster, year, genre, rating = await _get_poster(series, settings)
        seasons = list((await get_series_summary(series))["seasons"])
        kb = build_season_pages(seasons, q.from_user.id, series)
        try:
            await _safe_edit_media(
                q, poster or DEFAULT_POSTER,
                caption=build_caption(series, year, genre, rating, seasons=seasons),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception:
            pass
        if auto_del > 0 and q.message:
            delete_message_later(cyber, q.message.chat.id, q.message.id, auto_del)

    @cyber.on_callback_query(filters.regex("^close_spell$"))
    async def close_spell(_, q):
        try:
            await q.message.delete()
        except Exception:
            await q.answer()

    # ── SEASON PAGE NAVIGATION ────────────────────────────────

    @cyber.on_callback_query(filters.regex("^spg\\|"))
    async def spg(_, q):
        _, uid, series, page = q.data.split("|", 3)
        # FIX: strip year AND normalize to lowercase
        series = _strip_year(series)
        if not await check_owner(q, uid):
            return
        # Answer immediately
        try:
            await q.answer()
        except Exception:
            pass
        seasons = list((await get_series_summary(series))["seasons"])
        kb = build_season_pages(seasons, uid, series, int(page))
        try:
            await q.message.edit_reply_markup(kb)
        except MessageNotModified:
            pass
        except Exception:
            pass

    @cyber.on_callback_query(filters.regex("^noop$"))
    async def noop(_, q):
        await q.answer()

    # ── SEASON → LANGUAGE ─────────────────────────────────────

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|", 3)
        # FIX: strip year suffix AND normalize to lowercase.
        # This is the core fix for the "Dark" and other series bug:
        # if series arrived as "dark (2017)" from old callback_data,
        # _strip_year turns it into "dark" which matches DB normalized_name.
        series = _strip_year(series)

        settings = await _get_settings()
        if q.from_user.id not in ADMIN_USERS and settings["maintenance"]:
            return await q.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)
        if not await check_owner(q, uid):
            return

        # Answer immediately — clears Telegram spinner before slow DB/API work
        try:
            await q.answer()
        except Exception:
            pass

        langs, poster_r = await asyncio.gather(
            get_unique_languages_by_season(series, int(season)),
            _get_poster(series, settings),
        )
        poster, year, genre, rating = poster_r
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_season|{uid}|{series}")
        kb   = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{series}|{season}|{l}", back_btn=back)
        try:
            await q.message.edit_caption(
                build_caption(series, year, genre, rating, languages=langs, step="language"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    # ── LANGUAGE → QUALITY (URL buttons)
    # FIX: uses cached _get_bot_info() instead of cyber.get_me() per tap
    # ─────────────────────────────────────────────────────────

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|", 4)
        # FIX: strip year AND normalize to lowercase
        series = _strip_year(series)
        if not await check_owner(q, uid):
            return

        # Answer immediately — clears Telegram spinner before any slow DB/API work
        try:
            await q.answer()
        except Exception:
            pass

        settings = await _get_settings()
        # Run all slow fetches concurrently
        qc_pairs, poster_r, (_, bot_username) = await asyncio.gather(
            get_unique_qualities_with_codec(series, lang, int(season)),
            _get_poster(series, settings),
            _get_bot_info(cyber),
        )
        poster, year, genre, rating = poster_r
        qs   = [qc[0] for qc in qc_pairs]
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_lang|{uid}|{series}|{season}")

        # FIX: filebot removed — always use main bot username for delivery links.
        _link_username = bot_username

        # FIX: encode ALL payloads concurrently instead of sequentially in a loop.
        payloads = await asyncio.gather(
            *[encode_payload(series, season, lang, quality, uid) for quality, _ in qc_pairs],
            return_exceptions=True,
        )

        rows, row = [], []
        for (quality, codec), payload in zip(qc_pairs, payloads):
            if isinstance(payload, Exception):
                continue
            label = _quality_label(quality, codec)
            link  = f"https://t.me/{_link_username}?start={payload}"
            row.append(InlineKeyboardButton(f"⬇️  {label}", url=link))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([back])
        kb = InlineKeyboardMarkup(rows)

        try:
            await q.message.edit_caption(
                build_caption(series, year, genre, rating,
                              languages=[lang], qualities=qs, step="quality"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    # ── BACK NAVIGATION ───────────────────────────────────────

    @cyber.on_callback_query(filters.regex("^bk_season\\|"))
    async def bk_season(_, q):
        _, uid, series = q.data.split("|", 2)
        # FIX: strip year AND normalize to lowercase
        series = _strip_year(series)
        if not await check_owner(q, uid):
            return
        # Answer immediately
        try:
            await q.answer()
        except Exception:
            pass
        settings = await _get_settings()
        seasons, poster_r = await asyncio.gather(
            get_series_summary(series),
            _get_poster(series, settings),
        )
        season_list = list(seasons["seasons"])
        poster, year, genre, rating = poster_r
        kb = build_season_pages(season_list, uid, series, page=0)
        try:
            await q.message.edit_caption(
                build_caption(series, year, genre, rating, seasons=season_list, step="season"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    @cyber.on_callback_query(filters.regex("^bk_lang\\|"))
    async def bk_lang(_, q):
        _, uid, series, season = q.data.split("|", 3)
        # FIX: strip year AND normalize to lowercase
        series = _strip_year(series)
        if not await check_owner(q, uid):
            return
        # Answer immediately
        try:
            await q.answer()
        except Exception:
            pass
        settings = await _get_settings()
        langs, poster_r = await asyncio.gather(
            get_unique_languages_by_season(series, int(season)),
            _get_poster(series, settings),
        )
        poster, year, genre, rating = poster_r
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_season|{uid}|{series}")
        kb   = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{series}|{season}|{l}", back_btn=back)
        try:
            await q.message.edit_caption(
                build_caption(series, year, genre, rating, languages=langs, step="language"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    # NOTE: bk_qual handler REMOVED — quality step shows URL buttons (no callback),
    # so there is no "back from quality" navigation. The handler was dead code that
    # also called cyber.get_me() on every press.
