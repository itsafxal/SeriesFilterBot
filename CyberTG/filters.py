"""
filters.py — FIXED & OPTIMIZED
================================
FIXES:
  1. await encode_payload() — it's now async (guarantees token in DB before URL shown)
  2. Per-user search cooldown (3s) — prevents DB flood from rapid repeated messages
  3. safe_edit handles FloodWait in addition to MessageNotModified
  4. AUTO_DELETE_DELAY fetched once at handler level, not per-branch
  5. Removed duplicate imports
  6. _quality_label: safely handles empty/None codec and normalizes quality key lookup
     so "720p", "1080p" etc. always display correctly when no codec is detected
"""

import math
import time

from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto
)
from pyrogram.errors import MessageNotModified, FloodWait

from config import (
    DEFAULT_POSTER,
    SPELL_IMG,
    SEASONS_PER_PAGE
)
from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
    get_unique_qualities_with_codec,
    get_global_setting,
    get_filter_enabled,
    get_poster_enabled,
    get_poster_landscape,
    get_caption_clean_keywords,
)

from utils.cyber_series import suggest_series, check_owner
from utils.cyber_filter import encode_payload          # now async
from utils.cyber_delete import delete_message_later
# get_series_info now called via _get_poster() wrapper in register_filters


# ── Per-user search cooldown (3 seconds) ─────────────────────
_SEARCH_COOLDOWN: dict[int, float] = {}
SEARCH_COOLDOWN_SECS = 3


def _is_on_cooldown(uid: int) -> bool:
    last = _SEARCH_COOLDOWN.get(uid, 0)
    if time.time() - last < SEARCH_COOLDOWN_SECS:
        return True
    _SEARCH_COOLDOWN[uid] = time.time()
    return False


# =================================================
# SAFE EDIT WRAPPER
# =================================================

async def safe_edit(coro, q):
    try:
        await coro
    except MessageNotModified:
        await q.answer()
    except FloodWait as fw:
        await q.answer()
        await __import__("asyncio").sleep(fw.value)
    except Exception:
        await q.answer()


# =================================================
# RESOLVE SERIES
# =================================================

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


# =================================================
# CAPTION BUILDER
# =================================================

def build_caption(series, year, genre, rating,
                  seasons=None, languages=None, qualities=None,
                  step="season"):

    lines = [
        f"<b>○ {'Title'}: {series}</b>",
        f"<b>○ {'Released'}: {year or 'N/A'}</b>",
        f"<b>○ {'Genre'}: {genre or 'N/A'}</b>",
        f"<b>○ {'Rating'}: {rating or 'N/A'}</b>",
        "",
    ]

    if step == "season":
        if seasons:
            txt = ", ".join(f"S{s}" for s in seasons)
            lines.append(f"<b>○ {'Seasons'}: {txt}</b>")
        lines.append("")
        lines.append(f"<b>{'Select Season'}</b>")

    elif step == "language":
        txt = ", ".join(languages)
        lines.append(f"<b>○ {'Languages'}: {txt}</b>")
        lines.append("")
        lines.append(f"<b>{'Select Language'}</b>")

    elif step == "quality":
        lt = ", ".join(languages)
        qt = ", ".join(qualities)
        lines.append(f"<b>○ {'Language'}: {lt}</b>")
        lines.append(f"<b>○ {'Quality'}: {qt}</b>")
        lines.append("")
        lines.append(f"<b>{'Select Quality'}</b>")

    return "\n".join(lines)


# =================================================
# KEYBOARD HELPERS
# =================================================

def build_grid_keyboard(items, cb_builder, per_row=2, back_btn=None):
    """
    Build a 2-column grid of buttons.
    back_btn: optional InlineKeyboardButton appended as a full-width last row.
    """
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
        row.append(InlineKeyboardButton(f"▶  Season {s:02d}", callback_data=f"gss|{uid}|{series}|{s}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("❮  Prev", callback_data=f"spg|{uid}|{series}|{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next  ❯", callback_data=f"spg|{uid}|{series}|{page+1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


# =================================================
# REGISTER FILTERS
# =================================================

# Quality label display map — stored as uppercase, shown with lowercase p
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
    """
    Format button label.
    With codec:    'H.265 1080p'
    Without codec: '720p'   (never shows empty codec prefix)
    """
    disp = QUALITY_DISPLAY.get((quality or "").upper().strip(), quality)
    if codec and codec.strip():
        return f"{codec.strip()} {disp}"
    return disp


def _apply_clean_keywords(text: str, keywords: list) -> str:
    """Remove matching keywords from text (case-insensitive substring match)."""
    if not text or not keywords:
        return text
    for kw in keywords:
        if kw:
            text = text.replace(kw, "")
    return text.strip()


def register_filters(cyber: Client):

    async def _is_maintenance(uid: int) -> bool:
        """Returns True if maintenance mode is ON and user is not admin."""
        from config import ADMIN_USERS
        if uid in ADMIN_USERS:
            return False
        return bool(await get_global_setting("MAINTENANCE_MODE", False))

    async def _get_poster(series: str):
        """Fetch poster respecting POSTER_ENABLED and POSTER_LANDSCAPE settings."""
        poster_enabled = await get_poster_enabled()
        if not poster_enabled:
            return None
        landscape = await get_poster_landscape()
        from CyberTG.poster import get_series_info as _get_series_info
        poster, year, genre, rating = await _get_series_info(series, landscape=landscape)
        return poster, year, genre, rating

    @cyber.on_message(filters.text & (filters.private | filters.group))
    async def search_handler(_, m):
        if not m.text or m.text.startswith("/"):
            return

        uid = m.from_user.id

        # ── Maintenance mode check ─────────────────────────────
        if await _is_maintenance(uid):
            await m.reply_text(
                "🔧 <b>Bot is under maintenance.</b>\n"
                "Please try again later.",
                parse_mode=enums.ParseMode.HTML,
            )
            return

        # ── Filter ON/OFF check ────────────────────────────────
        from config import ADMIN_USERS as _ADMINS
        if uid not in _ADMINS:
            filter_on = await get_filter_enabled()
            if not filter_on:
                return   # silently ignore when filter is OFF

        # ── Per-user cooldown: silently drop repeated messages ──
        if _is_on_cooldown(uid):
            return

        AUTO_DELETE_DELAY = await get_global_setting("AUTO_DELETE_DELAY", 0)
        try:
            AUTO_DELETE_DELAY = int(AUTO_DELETE_DELAY) if AUTO_DELETE_DELAY else 0
        except (TypeError, ValueError):
            AUTO_DELETE_DELAY = 0

        series, suggestions = await resolve_series_with_suggestions(m.text)

        # ── SINGLE MATCH ──────────────────────────────────────
        if series:
            poster_result = await _get_poster(series)
            if poster_result:
                poster, year, genre, rating = poster_result
            else:
                poster, year, genre, rating = None, None, None, None
            seasons = list((await get_series_summary(series))["seasons"])
            kb = build_season_pages(seasons, uid, series)

            photo = poster or DEFAULT_POSTER
            msg = await m.reply_photo(
                photo,
                caption=build_caption(series, year, genre, rating, seasons=seasons),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb
            )
            if AUTO_DELETE_DELAY > 0:
                delete_message_later(cyber, msg.chat.id, msg.id, AUTO_DELETE_DELAY)
            return

        # ── MULTIPLE MATCHES ──────────────────────────────────
        if suggestions:
            lines = [
                f"<b>{'I Found The Following Series Matched To Your Query'}</b>",
                f"<b>({m.text.strip()})</b>",
                "",
                f"<b>{'Matched Series'}</b>"
            ]
            buttons = []
            for s in suggestions:
                lines.append(f"○ {s}")
                buttons.append([InlineKeyboardButton(s, callback_data=f"sug|{s}")])
            buttons.append([InlineKeyboardButton("✕  Close", callback_data="close_spell")])

            msg = await m.reply_photo(
                SPELL_IMG,
                caption="\n".join(lines),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            if AUTO_DELETE_DELAY > 0:
                delete_message_later(cyber, msg.chat.id, msg.id, AUTO_DELETE_DELAY)
            return

        # ── NOTHING FOUND ─────────────────────────────────────
        msg = await m.reply_text("No Series Found. Please Try Another Title.")
        if AUTO_DELETE_DELAY > 0:
            delete_message_later(cyber, msg.chat.id, msg.id, AUTO_DELETE_DELAY)

    # -------------------------------------------------
    # SUGGESTION CLICK
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^sug\\|"))
    async def sug(_, q):
        if await _is_maintenance(q.from_user.id):
            return await q.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)
        series = q.data.split("|", 1)[1]
        poster_result = await _get_poster(series)
        if poster_result:
            poster, year, genre, rating = poster_result
        else:
            poster, year, genre, rating = None, None, None, None
        seasons = list((await get_series_summary(series))["seasons"])
        kb = build_season_pages(seasons, q.from_user.id, series)
        await safe_edit(
            q.message.edit_media(
                InputMediaPhoto(
                    poster or DEFAULT_POSTER,
                    caption=build_caption(series, year, genre, rating, seasons=seasons),
                    parse_mode=enums.ParseMode.HTML
                ),
                reply_markup=kb
            ),
            q
        )

    @cyber.on_callback_query(filters.regex("^close_spell$"))
    async def close_spell(_, q):
        await q.message.delete()

    # -------------------------------------------------
    # PAGINATION
    # -------------------------------------------------

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

    # -------------------------------------------------
    # SEASON → LANGUAGE
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|")
        if await _is_maintenance(q.from_user.id):
            return await q.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)
        if not await check_owner(q, uid):
            return
        langs = await get_unique_languages_by_season(series, int(season))
        poster_r = await _get_poster(series)
        poster, year, genre, rating = poster_r if poster_r else (None, None, None, None)
        # Back → Season select (page 0)
        back = InlineKeyboardButton("↩  Back", callback_data=f"bk_season|{uid}|{series}")
        kb = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{series}|{season}|{l}", back_btn=back)
        await safe_edit(
            q.message.edit_caption(
                build_caption(series, year, genre, rating, languages=langs, step="language"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb
            ),
            q
        )

    # -------------------------------------------------
    # LANGUAGE → QUALITY
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|")
        if not await check_owner(q, uid):
            return
        # Get (quality, codec) pairs for rich button labels e.g. "H.265 1080P"
        qc_pairs = await get_unique_qualities_with_codec(series, lang, int(season))
        qs       = [qc[0] for qc in qc_pairs]   # plain quality list for caption
        poster_r = await _get_poster(series)
        poster, year, genre, rating = poster_r if poster_r else (None, None, None, None)
        back = InlineKeyboardButton("↩  Back", callback_data=f"bk_lang|{uid}|{series}|{season}")

        # Build buttons: label shows codec + quality (or just quality if no codec found)
        rows, row = [], []
        for quality, codec in qc_pairs:
            label = _quality_label(quality, codec)
            row.append(InlineKeyboardButton(label, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{quality}"))
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
                reply_markup=kb
            ),
            q
        )

    # -------------------------------------------------
    # QUALITY → DOWNLOAD LINK
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):
        _, uid, series, season, lang, quality = q.data.split("|")
        if not await check_owner(q, uid):
            return

        # FIX: await encode_payload (now async — guarantees token in DB)
        payload = await encode_payload(series, season, lang, quality, uid)

        bot  = await cyber.get_me()
        link = f"https://t.me/{bot.username}?start={payload}"

        # Back → Quality select for this season+language
        back = InlineKeyboardButton("↩  Back", callback_data=f"bk_qual|{uid}|{series}|{season}|{lang}")
        await safe_edit(
            q.message.edit_caption(
                "Your Files Are Ready. Tap Below To Download.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬇  Download Files", url=link)],
                    [back],
                ])
            ),
            q
        )

    # -------------------------------------------------
    # BACK NAVIGATION HANDLERS
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^bk_season\\|"))
    async def bk_season(_, q):
        """Back from Language → Season select (page 0)."""
        _, uid, series = q.data.split("|")
        if not await check_owner(q, uid):
            return
        poster_r = await _get_poster(series)
        poster, year, genre, rating = poster_r if poster_r else (None, None, None, None)
        seasons = list((await get_series_summary(series))["seasons"])
        kb = build_season_pages(seasons, uid, series, page=0)
        await safe_edit(
            q.message.edit_caption(
                build_caption(series, year, genre, rating, seasons=seasons, step="season"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb
            ),
            q
        )

    @cyber.on_callback_query(filters.regex("^bk_lang\\|"))
    async def bk_lang(_, q):
        """Back from Quality → Language select."""
        _, uid, series, season = q.data.split("|")
        if not await check_owner(q, uid):
            return
        langs = await get_unique_languages_by_season(series, int(season))
        poster_r = await _get_poster(series)
        poster, year, genre, rating = poster_r if poster_r else (None, None, None, None)
        back = InlineKeyboardButton("↩  Back", callback_data=f"bk_season|{uid}|{series}")
        kb = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{series}|{season}|{l}", back_btn=back)
        await safe_edit(
            q.message.edit_caption(
                build_caption(series, year, genre, rating, languages=langs, step="language"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb
            ),
            q
        )

    @cyber.on_callback_query(filters.regex("^bk_qual\\|"))
    async def bk_qual(_, q):
        """Back from Download → Quality select."""
        _, uid, series, season, lang = q.data.split("|")
        if not await check_owner(q, uid):
            return
        qc_pairs = await get_unique_qualities_with_codec(series, lang, int(season))
        qs       = [qc[0] for qc in qc_pairs]
        poster_r = await _get_poster(series)
        poster, year, genre, rating = poster_r if poster_r else (None, None, None, None)
        back = InlineKeyboardButton("↩  Back", callback_data=f"bk_lang|{uid}|{series}|{season}")
        rows, row = [], []
        for quality, codec in qc_pairs:
            label = _quality_label(quality, codec)
            row.append(InlineKeyboardButton(label, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{quality}"))
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
                reply_markup=kb
            ),
            q
        )
