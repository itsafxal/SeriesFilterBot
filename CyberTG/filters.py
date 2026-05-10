import math
import re
import time
import asyncio
from collections import OrderedDict
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto,
)
from pyrogram.errors import MessageNotModified, FloodWait, WebpageMediaEmpty
from config import (
    DEFAULT_POSTER, SPELL_IMG, SEASONS_PER_PAGE, ADMIN_USERS,
)
from DB.database import (
    search_series, get_series_summary, get_unique_languages_by_season,
    get_unique_qualities_with_codec, get_global_setting,
    get_filter_enabled, get_poster_enabled, get_poster_landscape,
)
from utils.cyber_series import suggest_series_with_year, check_owner
from utils.cyber_filter import encode_payload
from utils.cyber_delete import delete_message_later

# ── Series name registry (SID) ────────────────────────────────
_SID_MAP: dict[int, str] = {}
_SID_REVMAP: dict[str, int] = {}
_SID_NEXT: int = 1
_SID_MAX = 50_000

def _get_sid(series: str) -> int:
    global _SID_NEXT
    series = series.lower().strip()
    if series in _SID_REVMAP:
        return _SID_REVMAP[series]
    sid = _SID_NEXT
    _SID_NEXT += 1
    if len(_SID_MAP) >= _SID_MAX:
        oldest_sid = next(iter(_SID_MAP))
        oldest_name = _SID_MAP.pop(oldest_sid)
        _SID_REVMAP.pop(oldest_name, None)
    _SID_MAP[sid] = series
    _SID_REVMAP[series] = sid
    return sid

def _sid_to_series(sid_str: str) -> str:
    try:
        return _SID_MAP.get(int(sid_str), "")
    except (ValueError, TypeError):
        return sid_str.lower().strip()

# ── Year suffix stripper ──────────────────────────────────────
_YEAR_SUFFIX_RE = re.compile(r"\s*\(\d{4}\)\s*$")

def _strip_year(series: str) -> str:
    return _YEAR_SUFFIX_RE.sub("", series.lower()).strip()

# ── Settings TTL cache ────────────────────────────────────────
SETTINGS_TTL = 30
_settings_cache: dict = {}
_settings_ts: float = 0.0

async def _get_settings() -> dict:
    global _settings_cache, _settings_ts
    now = time.monotonic()
    if now - _settings_ts < SETTINGS_TTL and _settings_cache:
        return _settings_cache
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
    global _settings_ts
    _settings_ts = 0.0

# ── Per‑user search cooldown (capped) ─────────────────────────
_SEARCH_COOLDOWN: dict[int, float] = {}
SEARCH_COOLDOWN_SECS = 3
MAX_COOLDOWN_ENTRIES = 5000

def _is_on_cooldown(uid: int) -> bool:
    return (time.time() - _SEARCH_COOLDOWN.get(uid, 0)) < SEARCH_COOLDOWN_SECS

def _stamp_cooldown(uid: int):
    if len(_SEARCH_COOLDOWN) >= MAX_COOLDOWN_ENTRIES:
        try:
            _SEARCH_COOLDOWN.pop(next(iter(_SEARCH_COOLDOWN)))
        except StopIteration:
            pass
    _SEARCH_COOLDOWN[uid] = time.time()

# ── Safe reply/edit helpers ───────────────────────────────────
async def safe_edit(coro, q):
    try:
        await coro
    except (MessageNotModified, FloodWait):
        pass
    except Exception:
        pass
    try:
        await q.answer()
    except Exception:
        pass

async def _safe_reply_photo(m, photo, caption, parse_mode, reply_markup):
    if photo:
        try:
            return await m.reply_photo(photo, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
        except (WebpageMediaEmpty, Exception):
            pass
    return await m.reply_text(caption, parse_mode=parse_mode, reply_markup=reply_markup)

async def _safe_edit_media(q, photo, caption, parse_mode, reply_markup):
    if photo:
        try:
            return await q.message.edit_media(
                InputMediaPhoto(photo, caption=caption, parse_mode=parse_mode),
                reply_markup=reply_markup,
            )
        except (WebpageMediaEmpty, Exception):
            pass
    try:
        await q.message.edit_caption(caption, parse_mode=parse_mode, reply_markup=reply_markup)
    except (MessageNotModified, Exception):
        pass

# ── Poster helper ─────────────────────────────────────────────
async def _get_poster(series: str, settings: dict):
    if not settings.get("poster_enabled"):
        return None, None, None, None
    landscape = settings.get("poster_landscape", False)
    from CyberTG.poster import get_series_info
    return await get_series_info(series, landscape=landscape)

# ── Resolve series ────────────────────────────────────────────
async def resolve_series_with_suggestions(text: str):
    if not text:
        return None, []
    query = text.strip().lower()
    if not query:
        return None, []
    results = await search_series(query)
    if not isinstance(results, (list, tuple)):
        return None, []
    if not results:
        fuzzy = await suggest_series_with_year(query)
        if isinstance(fuzzy, (list, tuple)):
            return None, list(fuzzy)[:4]
        return None, []
    from utils.cyber_series import _normalize_title
    # Normalize every DB result and collapse variants that share the same
    # base title (e.g. "Stranger Things", "Stranger Things S05P2",
    # "Stranger Things Tales From" → all become "Stranger Things").
    seen_norm: dict[str, str] = {}   # normalized_lower → shortest raw title
    for raw in results:
        norm = _normalize_title(raw)
        k = norm.lower()
        if not k:
            continue
        # Keep the shortest normalized form as the canonical one
        if k not in seen_norm or len(norm) < len(seen_norm[k]):
            seen_norm[k] = norm

    # Second pass: collapse prefix duplicates (same logic as cyber_series)
    final_keys: list[str] = []
    for k in list(seen_norm.keys()):
        dominated = any(k.startswith(e) and k != e for e in final_keys)
        if dominated:
            continue
        # Replace any existing key that is a longer variant of this one
        final_keys = [e for e in final_keys if not e.startswith(k)]
        final_keys.append(k)

    unique = [seen_norm[k] for k in final_keys if k in seen_norm][:4]

    if len(unique) == 1:
        return unique[0], []
    return None, [(s, None) for s in unique]

# ── Caption & keyboard builders ───────────────────────────────
def build_caption(series, year, genre, rating, seasons=None, languages=None, qualities=None, step="season"):
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
            lines.append(f"📂  <b>Seasons</b>  <code>{' · '.join(f'S{s:02d}' for s in seasons)}</code>")
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

def build_grid_keyboard(items, cb_builder, per_row=2, back_btn=None):
    rows, row = [], []
    for it in items:
        if not it:
            continue
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
    sid = _get_sid(series)
    total_pages = math.ceil(len(seasons) / SEASONS_PER_PAGE)
    start = page * SEASONS_PER_PAGE
    chunk = seasons[start:start + SEASONS_PER_PAGE]
    rows, row = [], []
    for s in chunk:
        row.append(InlineKeyboardButton(f"Season {s:02d}", callback_data=f"gss|{uid}|{sid}|{s}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"spg|{uid}|{sid}|{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"spg|{uid}|{sid}|{page+1}"))
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

QUALITY_DISPLAY = {
    "4K": "4K", "2160P": "2160p", "1080P": "1080p",
    "720P": "720p", "480P": "480p", "360P": "360p", "UNKNOWN": "Unknown",
}
def _quality_label(quality: str, codec: str) -> str:
    disp = QUALITY_DISPLAY.get((quality or "").upper().strip(), quality)
    return f"{codec.strip()} {disp}" if codec and codec.strip() else disp

# ── Bot identity cache ────────────────────────────────────────
_bot_id_cache = None
_bot_username_cache = None

async def _get_bot_info(cyber):
    global _bot_id_cache, _bot_username_cache
    if _bot_id_cache is not None:
        return _bot_id_cache, _bot_username_cache
    try:
        me = await cyber.get_me()
        _bot_id_cache = me.id
        _bot_username_cache = me.username
        return _bot_id_cache, _bot_username_cache
    except Exception:
        return None, None

# ── Register all filter handlers ──────────────────────────────
def register_filters(cyber: Client):

    @cyber.on_message(filters.text & filters.incoming & (filters.private | filters.group))
    async def search_handler(_, m):
        if not m.text or m.text.startswith("/"):
            return
        bot_id, _ = await _get_bot_info(cyber)
        if bot_id and m.from_user and m.from_user.id == bot_id:
            return
        if m.reply_to_message and bot_id and m.reply_to_message.from_user and m.reply_to_message.from_user.id == bot_id:
            return
        uid = m.from_user.id
        settings = await _get_settings()
        if uid not in ADMIN_USERS and settings["maintenance"]:
            return await m.reply_text("🔧 <b>Bot is under maintenance.</b>", parse_mode=enums.ParseMode.HTML)
        if uid not in ADMIN_USERS and not settings["filter_enabled"]:
            return
        if _is_on_cooldown(uid):
            return
        auto_del = settings["auto_delete_delay"]
        series, suggestions = await resolve_series_with_suggestions(m.text)
        if series:
            series = _strip_year(series)
            poster, year, genre, rating = await _get_poster(series, settings)
            seasons = list((await get_series_summary(series))["seasons"])
            kb = build_season_pages(seasons, uid, series)
            photo = poster or DEFAULT_POSTER
            msg = await _safe_reply_photo(
                m, photo,
                build_caption(series, year, genre, rating, seasons=seasons, step="season"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
            _stamp_cooldown(uid)
            if auto_del > 0 and msg:
                delete_message_later(cyber, msg.chat.id, msg.id, auto_del)
            return
        if suggestions:
            from utils.cyber_series import format_suggestion_label
            lines = [
                "🔍 <b>Did you mean...</b>",
                "<b>━━━━━━━━━━━━━━━━━━━━━</b>",
                "",
                f"Found <b>{len(suggestions)}</b> titles matching <code>{m.text.strip()}</code>",
                "",
                "Tap a title to select it ↓",
            ]
            buttons = []
            for title, yr in suggestions:
                display = format_suggestion_label(title, yr)
                clean = _strip_year(title)
                buttons.append([InlineKeyboardButton(f"🎬  {display}", callback_data=f"sug|{uid}|{clean}")])
            buttons.append([InlineKeyboardButton("✖️  Close", callback_data="close_spell")])
            msg = await _safe_reply_photo(
                m, SPELL_IMG, "\n".join(lines),
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
            delete_message_later(cyber, m.chat.id, m.id, auto_del)
            delete_message_later(cyber, msg.chat.id, msg.id, auto_del)

    @cyber.on_callback_query(filters.regex("^sug\\|"))
    async def sug(_, q):
        settings = await _get_settings()
        if q.from_user.id not in ADMIN_USERS and settings["maintenance"]:
            return await q.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)
        parts = q.data.split("|", 2)
        if len(parts) == 3:
            _, uid_str, raw_series = parts
            if not await check_owner(q, uid_str):
                return
        else:
            raw_series = parts[1]
        await q.answer()
        series = _strip_year(raw_series)
        auto_del = settings["auto_delete_delay"]
        poster, year, genre, rating = await _get_poster(series, settings)
        seasons = list((await get_series_summary(series))["seasons"])
        owner_uid = int(uid_str) if len(parts) == 3 else q.from_user.id
        kb = build_season_pages(seasons, owner_uid, series)
        await _safe_edit_media(
            q, poster or DEFAULT_POSTER,
            build_caption(series, year, genre, rating, seasons=seasons, step="season"),
            enums.ParseMode.HTML, kb,
        )
        if auto_del > 0 and q.message:
            delete_message_later(cyber, q.message.chat.id, q.message.id, auto_del)

    @cyber.on_callback_query(filters.regex("^close_spell$"))
    async def close_spell(_, q):
        try:
            await q.message.delete()
        except Exception:
            await q.answer()

    @cyber.on_callback_query(filters.regex("^spg\\|"))
    async def spg(_, q):
        _, uid, sid_or_series, page = q.data.split("|", 3)
        series = _sid_to_series(sid_or_series)
        if not series:
            return await q.answer("Session expired. Please search again.", show_alert=True)
        series = _strip_year(series)
        if not await check_owner(q, uid):
            return
        await q.answer()
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

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, sid_or_series, season = q.data.split("|", 3)
        series = _sid_to_series(sid_or_series)
        if not series:
            return await q.answer("Session expired. Please search again.", show_alert=True)
        series = _strip_year(series)
        settings = await _get_settings()
        if q.from_user.id not in ADMIN_USERS and settings["maintenance"]:
            return await q.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)
        if not await check_owner(q, uid):
            return
        await q.answer()
        langs, poster_r = await asyncio.gather(
            get_unique_languages_by_season(series, int(season)),
            _get_poster(series, settings),
        )
        poster, year, genre, rating = poster_r
        sid_new = _get_sid(series)
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_season|{uid}|{sid_new}")
        kb = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{sid_new}|{season}|{l}", back_btn=back)
        cap = build_caption(series, year, genre, rating, languages=langs, step="language")
        try:
            await q.message.edit_caption(cap, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
        except MessageNotModified:
            pass
        except Exception as e:
            import logging as _log
            _log.getLogger("BOT").error(f"[gss] edit_caption failed: {e}")
            try:
                await q.message.edit_text(cap, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, sid_or_series, season, lang = q.data.split("|", 4)
        series = _sid_to_series(sid_or_series)
        if not series:
            return await q.answer("Session expired. Please search again.", show_alert=True)
        series = _strip_year(series)
        if not await check_owner(q, uid):
            return
        await q.answer()
        settings = await _get_settings()
        qc_pairs, poster_r, (_, bot_username) = await asyncio.gather(
            get_unique_qualities_with_codec(series, lang, int(season)),
            _get_poster(series, settings),
            _get_bot_info(cyber),
        )
        poster, year, genre, rating = poster_r
        qs = [qc[0] for qc in qc_pairs]
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_lang|{uid}|{series}|{season}")
        _link_username = bot_username or "YourBot"
        payloads = await asyncio.gather(
            *[encode_payload(series, season, lang, quality, uid) for quality, _ in qc_pairs],
            return_exceptions=True,
        )
        rows, row = [], []
        for (quality, codec), payload in zip(qc_pairs, payloads):
            if isinstance(payload, Exception):
                continue
            label = _quality_label(quality, codec)
            link = f"https://t.me/{_link_username}?start={payload}"
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
                build_caption(series, year, genre, rating, languages=[lang], qualities=qs, step="quality"),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb,
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    @cyber.on_callback_query(filters.regex("^bk_season\\|"))
    async def bk_season(_, q):
        _, uid, sid_or_series = q.data.split("|", 2)
        series = _sid_to_series(sid_or_series)
        if not series:
            return await q.answer("Session expired. Please search again.", show_alert=True)
        series = _strip_year(series)
        if not await check_owner(q, uid):
            return
        await q.answer()
        settings = await _get_settings()
        seasons, poster_r = await asyncio.gather(
            get_series_summary(series),
            _get_poster(series, settings),
        )
        season_list = list(seasons["seasons"])
        poster, year, genre, rating = poster_r
        kb = build_season_pages(season_list, uid, series, page=0)
        cap = build_caption(series, year, genre, rating, seasons=season_list, step="season")
        try:
            await q.message.edit_caption(cap, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
        except MessageNotModified:
            pass
        except Exception as _e:
            import logging as _log
            _log.getLogger("BOT").error(f"[bk_season] edit_caption failed: {_e}")
            try:
                await q.message.edit_text(cap, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass

    @cyber.on_callback_query(filters.regex("^bk_lang\\|"))
    async def bk_lang(_, q):
        _, uid, sid_or_series, season = q.data.split("|", 3)
        series = _sid_to_series(sid_or_series)
        if not series:
            return await q.answer("Session expired. Please search again.", show_alert=True)
        series = _strip_year(series)
        if not await check_owner(q, uid):
            return
        await q.answer()
        settings = await _get_settings()
        langs, poster_r = await asyncio.gather(
            get_unique_languages_by_season(series, int(season)),
            _get_poster(series, settings),
        )
        poster, year, genre, rating = poster_r
        sid_new = _get_sid(series)
        back = InlineKeyboardButton("⬅️  Back", callback_data=f"bk_season|{uid}|{sid_new}")
        kb = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{sid_new}|{season}|{l}", back_btn=back)
        cap = build_caption(series, year, genre, rating, languages=langs, step="language")
        try:
            await q.message.edit_caption(cap, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
        except MessageNotModified:
            pass
        except Exception as _e:
            import logging as _log
            _log.getLogger("BOT").error(f"[bk_lang] edit_caption failed: {_e}")
            try:
                await q.message.edit_text(cap, parse_mode=enums.ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
