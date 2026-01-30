import asyncio
import base64
import re
import shelve
from urllib.parse import quote

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    InputMediaPhoto,
)

from config import (
    logger,
    DEFAULT_POSTER,
    OMDB_CACHE_DB,
    SPELL_IMG,
)

from utils import clean_series_name, delete_message_later
from DB.database import (
    get_global_setting,
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from .poster import get_series_poster_async

try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False


# ---------------------------------------------------------
# DEEP LINK ENCODER
# ---------------------------------------------------------
def _encode_payload(series, season, lang, quality, owner_id):
    raw = f"{owner_id}|{series}|{season}|{lang}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"send_{quote(b64)}"


# ---------------------------------------------------------
# POSTER
# ---------------------------------------------------------
async def _build_poster_for_series(series):
    poster = DEFAULT_POSTER

    try:
        with shelve.open(OMDB_CACHE_DB or "/tmp/omdb_cache.db") as db:
            key = f"poster::{series.lower()}"
            if key in db:
                poster = db[key]
    except Exception:
        pass

    try:
        new = await get_series_poster_async(series)
        if new:
            poster = new
    except Exception:
        pass

    return poster


# ---------------------------------------------------------
# CLEAN QUERY
# ---------------------------------------------------------
def _clean_query(q):
    return re.sub(
        r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|movie(s)?|latest|file|full)",
        "",
        q,
        flags=re.IGNORECASE,
    ).strip()


# ---------------------------------------------------------
# FUZZY
# ---------------------------------------------------------
def _fuzzy_match(query, candidates, limit=5):
    if not candidates:
        return []

    if HAVE_RAPIDFUZZ:
        res = rf_process.extract(query, candidates, scorer=rf_fuzz.ratio, limit=limit)
        return [x[0] for x in res if x[1] >= 30]

    scored = []
    for c in candidates:
        score = sum(ch in c.lower() for ch in query.lower())
        if score > 2:
            scored.append(c)
    return scored[:limit]


# ---------------------------------------------------------
# STRICT
# ---------------------------------------------------------
def _strict_match_only(query, names):
    q = query.lower().strip()
    return [n for n in names if n.lower().strip() == q]


# ---------------------------------------------------------
# MAIN REGISTER
# ---------------------------------------------------------
def register_groupfilter_handlers(cyber: Client):

    # ---------------- GROUP SEARCH ----------------
    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m: Message):

        text = (m.text or "").strip()
        if not text or text.startswith("/") or "http" in text:
            return

        try:
            results = await search_series(text)
        except Exception:
            results = []

        results = [clean_series_name(r) for r in results]

        strict = _strict_match_only(text, results)
        if strict:
            return await _send_series_entry(cyber, m, strict[0])

        cleaned = _clean_query(text)
        cleaned_results = []
        if cleaned:
            cleaned_results = await search_series(cleaned)

        cleaned_results = [clean_series_name(r) for r in cleaned_results]

        strict2 = _strict_match_only(cleaned, cleaned_results)
        if strict2:
            return await _send_series_entry(cyber, m, strict2[0])

        candidates = list(set(results + cleaned_results))

        if not candidates:
            msg = await m.reply_text(f"❌ No series found `<code>{text}</code>`", parse_mode=enums.ParseMode.HTML)
            asyncio.create_task(delete_message_later(cyber, msg.chat.id, msg.id, 20))
            return

        sugg = _fuzzy_match(text, candidates, 3)

        kb = [[InlineKeyboardButton(s, callback_data=f"gsugg|{m.from_user.id}|{s}")] for s in sugg]
        kb.append([InlineKeyboardButton("Close", callback_data=f"close_suggestion|{m.from_user.id}")])

        sent = await m.reply_photo(
            SPELL_IMG,
            caption=f"Couldn't find `<code>{text}</code>`\nDid you mean?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.HTML,
        )

        asyncio.create_task(delete_message_later(cyber, sent.chat.id, sent.id, 30))


    # ---------------- SUGGEST SELECT ----------------
    @cyber.on_callback_query(filters.regex("^gsugg\\|"))
    async def gsugg(_, q: CallbackQuery):
        _, uid, series = q.data.split("|", 2)
        if q.from_user.id != int(uid):
            return await q.answer("Not for you", show_alert=True)

        await q.answer()
        await _edit_to_series_entry(cyber, q, series)


    @cyber.on_callback_query(filters.regex("^close_suggestion"))
    async def close(_, q):
        try:
            await q.message.delete()
        except:
            pass
        await q.answer()


    # ---------------- SEASON ----------------
    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        season = int(season)
        langs = await get_unique_languages_by_season(series, season)

        kb = [[InlineKeyboardButton(l, callback_data=f"gls|{uid}|{series}|{season}|{l}")] for l in langs]
        kb.append([InlineKeyboardButton("↺ Back", callback_data=f"gback|{uid}|{series}")])

        await q.message.edit_caption("🌐 Select Language", reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()


    # ---------------- LANGUAGE ----------------
    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        qualities = await get_unique_qualities_by_lang_season(series, lang, int(season))

        kb = [[InlineKeyboardButton(qy, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{qy}")] for qy in qualities]
        kb.append([InlineKeyboardButton("↺ Back", callback_data=f"gss|{uid}|{series}|{season}")])

        await q.message.edit_caption("🎞 Select Quality", reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()


    # ---------------- QUALITY → DEEPLINK ----------------
    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):
        _, uid, series, season, lang, quality = q.data.split("|")

        if q.from_user.id != int(uid):
            return await q.answer("Not for you", show_alert=True)

        payload = _encode_payload(series, season, lang, quality, uid)
        bot = await cyber.get_me()

        await q.answer(url=f"https://t.me/{bot.username}?start={payload}")


    # ---------------- BACK ----------------
    @cyber.on_callback_query(filters.regex("^gback\\|"))
    async def gback(_, q):
        _, uid, series = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        await _edit_to_series_entry(cyber, q, series)


# ---------------------------------------------------------
# SEND SERIES ENTRY
# ---------------------------------------------------------
async def _send_series_entry(cyber, m, series):

    poster = await _build_poster_for_series(series)
    summary = await get_series_summary(series)

    caption = (
        f"**○ Title:** {series}\n"
        f"**○ Language:** {', '.join(summary.get('languages', []))}\n"
        f"**○ Seasons:** {len(summary.get('seasons', []))}\n"
        f"**○ Quality:** {', '.join(summary.get('qualities', []))}\n\n"
        f"Select Season 👇"
    )

    uid = m.from_user.id

    kb = [[InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{uid}|{series}|{s}")]
          for s in summary.get("seasons", [])]

    sent = await m.reply_photo(
        poster,
        caption=caption,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=enums.ParseMode.MARKDOWN
    )

    t = await get_global_setting("AUTO_DELETE_TIMER", 0)
    if t:
        asyncio.create_task(delete_message_later(cyber, sent.chat.id, sent.id, int(t)))


# ---------------------------------------------------------
# EDIT FROM SUGGESTION
# ---------------------------------------------------------
async def _edit_to_series_entry(cyber, q, series):

    poster = await _build_poster_for_series(series)
    summary = await get_series_summary(series)

    caption = (
        f"**○ Title:** {series}\n"
        f"**○ Language:** {', '.join(summary.get('languages', []))}\n"
        f"**○ Seasons:** {len(summary.get('seasons', []))}\n"
        f"**○ Quality:** {', '.join(summary.get('qualities', []))}\n\n"
        f"Select Season 👇"
    )

    uid = q.from_user.id

    kb = [[InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{uid}|{series}|{s}")]
          for s in summary.get("seasons", [])]

    try:
        await q.message.edit_media(
            InputMediaPhoto(poster, caption=caption, parse_mode=enums.ParseMode.MARKDOWN),
            reply_markup=InlineKeyboardMarkup(kb)
        )
    except:
        await q.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(kb))

    await q.answer()
