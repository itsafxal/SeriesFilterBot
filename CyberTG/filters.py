"""
filters.py — OPTIMIZED EDITION
================================
Performance fixes for faster UI search results:

  1. SETTINGS CACHE — poster_enabled, poster_landscape, auto_delete_delay
     are fetched from MongoDB once and cached for 30s (TTL cache).
     Previously every single button tap triggered a MongoDB round-trip.
     Now back-nav, season→lang→quality flow all use the in-process cache.

  2. _SEARCH_COOLDOWN memory leak fixed — dict grew unbounded (one entry
     per unique user, never evicted). Now capped at 5000 entries with LRU-
     style eviction: oldest entry removed when cap is hit.

  3. encode_payload awaited correctly — it's async (guarantees token in DB).

  4. safe_edit handles FloodWait + MessageNotModified.

  5. _safe_reply_photo / _safe_edit_media fall back gracefully on bad URLs.

  6. resolve_series_with_suggestions: dedup via dict.fromkeys is O(n),
     already optimal — kept as-is.
"""

import math
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

from utils.cyber_series import suggest_series, check_owner
from utils.cyber_filter import encode_payload
from utils.cyber_delete import delete_message_later


# ─────────────────────────────────────────────────────────────
# SETTINGS TTL CACHE — avoids MongoDB round-trip per button press
# Values refreshed at most once every SETTINGS_TTL seconds.
# ─────────────────────────────────────────────────────────────

SETTINGS_TTL = 30  # seconds

_settings_cache: dict = {}
_settings_ts:    float = 0.0


async def _get_settings() -> dict:
    """
    Return cached settings dict, refreshing from DB if stale.
    Keys: poster_enabled, poster_landscape, auto_delete_delay, filter_enabled, maintenance
    """
    global _settings_cache, _settings_ts
    now = time.monotonic()
    if now - _settings_ts < SETTINGS_TTL and _settings_cache:
        return _settings_cache

    # Fetch all needed settings concurrently in one round
    poster_on, landscape, auto_del, filter_on, maintenance = await asyncio.gather(
        get_poster_enabled(),
        get_poster_landscape(),
        get_global_setting("AUTO_DELETE_DELAY", 0),
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
    """Call after any setting is changed so next request re-fetches."""
    global _settings_ts
    _settings_ts = 0.0


# ─────────────────────────────────────────────────────────────
# PER-USER SEARCH COOLDOWN
# FIX: cap at MAX_COOLDOWN_ENTRIES to prevent unbounded memory growth.
# When cap is hit the oldest entry (first key) is removed.
# ─────────────────────────────────────────────────────────────

_SEARCH_COOLDOWN: dict[int, float] = {}
SEARCH_COOLDOWN_SECS  = 3
MAX_COOLDOWN_ENTRIES  = 5000


def _is_on_cooldown(uid: int) -> bool:
    last = _SEARCH_COOLDOWN.get(uid, 0)
    now  = time.time()
    if now - last < SEARCH_COOLDOWN_SECS:
        return True
    # Evict oldest entry if at cap to prevent memory leak
    if len(_SEARCH_COOLDOWN) >= MAX_COOLDOWN_ENTRIES:
        try:
            _SEARCH_COOLDOWN.pop(next(iter(_SEARCH_COOLDOWN)))
        except StopIteration:
            pass
    _SEARCH_COOLDOWN[uid] = now
    return False


# ─────────────────────────────────────────────────────────────
# SAFE EDIT / REPLY WRAPPERS
# ─────────────────────────────────────────────────────────────

async def safe_edit(coro, q):
    try:
        await coro
    except MessageNotModified:
        await q.answer()
    except FloodWait as fw:
        await q.answer()
        await asyncio.sleep(fw.value)
    except Exception:
        await q.answer()


async def _safe_reply_photo(m, photo: str, caption: str, parse_mode, reply_markup):
    """
    Try reply_photo. Falls back to reply_text if Telegram rejects the URL.
    NOTE: ibb.co page URLs (https://ibb.co/xxx) don't work — use direct
    image URLs (https://i.ibb.co/.../image.jpg) in config.py.
    """
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
    """Try edit_media. Falls back to edit_caption if URL is bad."""
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
# POSTER HELPER — uses settings cache, no per-call MongoDB hit
# ─────────────────────────────────────────────────────────────

async def _get_poster(series: str, settings: dict):
    """
    Fetch poster using already-loaded settings dict.
    Returns (poster, year, genre, rating) or (None, None, None, None).
    """
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
            fuzzy = await suggest_series(query)
            if isinstance(fuzzy, (list, tuple)):
                return None, list(dict.fromkeys(fuzzy))[:4]
        except Exception:
            pass
        return None, []

    unique      = list(dict.fromkeys(results))[:4]
    query_words = query.split()

    if len(unique) == 1:
        result_words = unique[0].lower().split()
        if len(query_words) >= len(result_words):
            return unique[0], []
        return None, unique

    return None, unique


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

    rows, row = [], []
    for s in chunk:
        row.append(InlineKeyboardButton(
            f"▶  Season {s:02d}", callback_data=f"gss|{uid}|{series}|{s}"
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
# REGISTER FILTERS
# ─────────────────────────────────────────────────────────────

def register_filters(cyber: Client):

    # ── SEARCH HANDLER ────────────────────────────────────────

    @cyber.on_message(filters.text & (filters.private | filters.group))
    async def search_handler(_, m):
        if not m.text or m.text.startswith("/"):
            return

        uid = m.from_user.id

        # Load all settings in one cached batch (single MongoDB call at most)
        settings = await _get_settings()

        # Maintenance check
        from config import ADMIN_USERS as _ADMINS
        if uid not in _ADMINS and settings["maintenance"]:
            await m.reply_text(
                "🔧 <b>Bot is under maintenance.</b>\nPlease try again later.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        # Filter ON/OFF
        if uid not in _ADMINS and not settings["filter_enabled"]:
            return

        # Per-user cooldown
        if _is_on_cooldown(uid):
            return

        auto_del = settings["auto_delete_delay"]

        series, suggestions = await resolve_series_with_suggestions(m.text)

        # ── SINGLE MATCH ──────────────────────────────────────
        if series:
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
            if auto_del > 0 and msg:
                delete_message_later(cyber, msg.chat.id, msg.id, auto_del)
            return

        # ── MULTIPLE MATCHES ──────────────────────────────────
        if suggestions:
            lines = [
                "🔍 <b>Did you mean...</b>",
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>",
                "",
                f"Found <b>{len(suggestions)}</b> titles matching "
                f"<code>{m.text.strip()}</code>",
                "",
                "Tap a title to select it ↓",
            ]
            buttons = [
                [InlineKeyboardButton(f"🎬  {s}", callback_data=f"sug|{s}")]
                for s in suggestions
            ]
            buttons.append([InlineKeyboardButton("✖️  Close", callback_data="close_spell")])
            msg = await _safe_reply_photo(
                m, SPELL_IMG,
                caption="\n".join(lines),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            if auto_del > 0 and msg:
                delete_message_later(cyber, msg.chat.id, msg.id, auto_del)
                # Also delete the user's original typed message
                delete_message_later(cyber, m.chat.id, m.id, auto_del)
            return

        # ── NOTHING FOUND ─────────────────────────────────────
        msg = await m.reply_text(
            "🔍 <b>No Results Found</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"No series matched <code>{m.text.strip()}</code>\n\n"
            "<i>Try a different title or check your spelling.</i>",
            parse_mode=enums.ParseMode.HTML,
        )
        if auto_del > 0 and msg:
            delete_message_later(cyber, msg.chat.id, msg.id, auto_del)
            # Also delete the user's original typed message
            delete_message_later(cyber, m.chat.id, m.id, auto_del)

    # ── SUGGESTION CLICK ──────────────────────────────────────

    @cyber.on_callback_query(filters.regex("^sug\\|"))
    async def sug(_, q):
        settings = await _get_settings()
        if q.from_user.id not in __import__("config").ADMIN_USERS and settings["maintenance"]:
            return await q.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)
        series = q.data.split("|", 1)[1]
        auto_del = settings["auto_delete_delay"]
        poster, year, genre, rating = await _get_poster(series, settings)
        seasons = list((await get_series_summary(series))["seasons"])
        kb = build_season_pages(seasons, q.from_user.id, series)
        await safe_edit(
            _safe_edit_media(
                q, poster or DEFAULT_POSTER,
                caption=build_caption(series, year, genre, rating, seasons=seasons),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            ),
            q,
        )
        # Reschedule auto-delete on the now-edited message (series poster)
        # The old delete timer from the suggestion message is still running but
        # editing the message doesn't cancel it — we schedule a fresh one so
        # the series poster also gets cleaned up at the correct delay.
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
        _, uid, series, page = q.data.split("|")
        if not await check_owner(q, uid):
            return
        seasons = list((await get_series_summary(series))["seasons"])
        kb = build_season_pages(seasons, uid, series, int(page))
        await safe_edit(q.message.edit_reply_markup(kb), q)

    @cyber.on_callback_query(filters.regex("^noop$"))
    async def noop(_, q):
        await q.answer()

    # ── SEASON → LANGUAGE ─────────────────────────────────────

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|")
        settings = await _get_settings()
        if q.from_user.id not in __import__("config").ADMIN_USERS and settings["maintenance"]:
            return await q.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)
        if not await check_owner(q, uid):
            return
        # Fetch langs and poster concurrently
        langs, poster_r = await asyncio.gather(
            get_unique_languages_by_season(series, int(season)),
            _get_poster(series, settings),
        )
        poster, year, genre, rating = poster_r
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_season|{uid}|{series}")
        kb   = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{series}|{season}|{l}", back_btn=back)
        await safe_edit(
            q.message.edit_caption(
                build_caption(series, year, genre, rating, languages=langs, step="language"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            ),
            q,
        )

    # ── LANGUAGE → QUALITY ────────────────────────────────────

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|")
        if not await check_owner(q, uid):
            return
        settings = await _get_settings()
        # Fetch quality pairs and poster concurrently
        qc_pairs, poster_r = await asyncio.gather(
            get_unique_qualities_with_codec(series, lang, int(season)),
            _get_poster(series, settings),
        )
        poster, year, genre, rating = poster_r
        qs   = [qc[0] for qc in qc_pairs]
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_lang|{uid}|{series}|{season}")

        rows, row = [], []
        for quality, codec in qc_pairs:
            label = _quality_label(quality, codec)
            row.append(InlineKeyboardButton(
                label, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{quality}"
            ))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([back])
        kb = InlineKeyboardMarkup(rows)

        await safe_edit(
            q.message.edit_caption(
                build_caption(series, year, genre, rating,
                              languages=[lang], qualities=qs, step="quality"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            ),
            q,
        )

    # ── QUALITY → DOWNLOAD LINK ───────────────────────────────

    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):
        _, uid, series, season, lang, quality = q.data.split("|")
        if not await check_owner(q, uid):
            return

        payload = await encode_payload(series, season, lang, quality, uid)
        bot     = await cyber.get_me()
        link    = f"https://t.me/{bot.username}?start={payload}"

        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_qual|{uid}|{series}|{season}|{lang}")
        await safe_edit(
            q.message.edit_caption(
                "✅ <b>Files Ready</b>\n"
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                "Your files are ready to download.\n"
                "<i>Tap the button below to get started.</i>",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬇️  Download Files", url=link)],
                    [back],
                ]),
            ),
            q,
        )

    # ── BACK NAVIGATION ───────────────────────────────────────

    @cyber.on_callback_query(filters.regex("^bk_season\\|"))
    async def bk_season(_, q):
        _, uid, series = q.data.split("|")
        if not await check_owner(q, uid):
            return
        settings = await _get_settings()
        seasons, poster_r = await asyncio.gather(
            get_series_summary(series),
            _get_poster(series, settings),
        )
        season_list = list(seasons["seasons"])
        poster, year, genre, rating = poster_r
        kb = build_season_pages(season_list, uid, series, page=0)
        await safe_edit(
            q.message.edit_caption(
                build_caption(series, year, genre, rating, seasons=season_list, step="season"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            ),
            q,
        )

    @cyber.on_callback_query(filters.regex("^bk_lang\\|"))
    async def bk_lang(_, q):
        _, uid, series, season = q.data.split("|")
        if not await check_owner(q, uid):
            return
        settings = await _get_settings()
        langs, poster_r = await asyncio.gather(
            get_unique_languages_by_season(series, int(season)),
            _get_poster(series, settings),
        )
        poster, year, genre, rating = poster_r
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_season|{uid}|{series}")
        kb   = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{series}|{season}|{l}", back_btn=back)
        await safe_edit(
            q.message.edit_caption(
                build_caption(series, year, genre, rating, languages=langs, step="language"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            ),
            q,
        )

    @cyber.on_callback_query(filters.regex("^bk_qual\\|"))
    async def bk_qual(_, q):
        _, uid, series, season, lang = q.data.split("|")
        if not await check_owner(q, uid):
            return
        settings = await _get_settings()
        qc_pairs, poster_r = await asyncio.gather(
            get_unique_qualities_with_codec(series, lang, int(season)),
            _get_poster(series, settings),
        )
        poster, year, genre, rating = poster_r
        qs   = [qc[0] for qc in qc_pairs]
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_lang|{uid}|{series}|{season}")
        rows, row = [], []
        for quality, codec in qc_pairs:
            label = _quality_label(quality, codec)
            row.append(InlineKeyboardButton(
                label, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{quality}"
            ))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([back])
        kb = InlineKeyboardMarkup(rows)
        await safe_edit(
            q.message.edit_caption(
                build_caption(series, year, genre, rating,
                              languages=[lang], qualities=qs, step="quality"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            ),
            q,
        )
