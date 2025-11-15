# groupfilter.py
# FINAL VERSION WITH STRICT MATCH + FUZZY SUGGESTIONS + NO FALSE AUTO-SELECT
# WEDNESDA -> SUGGESTIONS (NOT AUTO Wednesday)

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
)

from config import (
    logger,
    DEFAULT_POSTER,
    MAX_CALLBACK_LENGTH,
    OMDB_CACHE_DB,
)

from utils import clean_series_name, delete_message_later
from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)
from .poster import get_series_poster_async

# ----------------------------
# Try RapidFuzz
# ----------------------------
try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except:
    HAVE_RAPIDFUZZ = False


# ---------------------------------------------------------
# BUILD POSTER
# ---------------------------------------------------------
async def _build_poster_for_series(series_name: str):
    poster = DEFAULT_POSTER
    try:
        cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
        with shelve.open(cache_file) as cache:
            key = f"poster::{series_name.lower()}"
            if key in cache:
                poster = cache[key]
    except:
        pass

    try:
        new = await get_series_poster_async(series_name)
        if new:
            poster = new
    except:
        pass

    return poster


# ---------------------------------------------------------
# DEEP LINK
# ---------------------------------------------------------
def _encode_payload(series, season, lang, quality):
    raw = f"{series}|{season}|{lang}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"send_{quote(b64)}"


# ---------------------------------------------------------
# CLEAN QUERY
# ---------------------------------------------------------
def _clean_query(q: str):
    return re.sub(
        r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\sme)?)|movie(s)?|new|"
        r"latest|br((o|u)h?)*|^h(e|a)?(l)*(o)*|mal(ayalam)?|t(h)?amil|file|that|find|und(o)*|"
        r"kit(t(i|y)?)?o(w)?|thar(u)?(o)*w?|kittum(o)*|aya(k)*(um(o)*)?|full\smovie|any(one)|"
        r"with\ssubtitle(s)?)",
        "",
        q,
        flags=re.IGNORECASE,
    ).strip()


# ---------------------------------------------------------
# FUZZY MATCHING
# ---------------------------------------------------------
def _fuzzy_match(query: str, candidates: list, limit=5):
    if not candidates:
        return []

    if HAVE_RAPIDFUZZ:
        res = rf_process.extract(
            query,
            candidates,
            scorer=rf_fuzz.ratio,
            limit=limit
        )
        return [(x[0], x[1]) for x in res if x[1] >= 30]
    else:
        # simple fallback
        scored = []
        ql = query.lower()
        for c in candidates:
            cl = c.lower()
            m = sum(ch in cl for ch in ql)
            score = (m / (len(ql) + len(cl))) * 200
            if score >= 30:
                scored.append((c, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]


# ---------------------------------------------------------
# STRICT MATCH CHECK
# ---------------------------------------------------------
def _strict_match_only(query: str, names: list):
    """
    Return only exact or strict-prefix matches.
    Wednesday.startswith("wednesda") == False → NOT strict
    Wednesday.startswith("wednes") == True → strict
    """
    q = query.lower().strip()
    matches = []
    for n in names:
        nl = n.lower().strip()
        if nl == q or nl.startswith(q):
            matches.append(n)
    return matches


# ---------------------------------------------------------
# MAIN REGISTRATION
# ---------------------------------------------------------
def register_groupfilter_handlers(cyber: Client):

    # -----------------------------------------------------
    # GROUP TEXT SEARCH
    # -----------------------------------------------------
    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m: Message):

        text = (m.text or "").strip()
        if not text or len(text) < 2:
            return

        if text.startswith("/") or "http" in text:
            return

        # 1) Search exact/loose from DB
        try:
            results = await search_series(text)
        except:
            results = []

        results = [clean_series_name(r) for r in results]

        # 2) STRICT MATCH FILTER
        strict = _strict_match_only(text, results)

        if strict:
            # We have a strict match → AUTO SELECT
            series = strict[0]
            return await _send_series_entry(cyber, m, series)

        # No strict match → DO NOT AUTO SELECT
        # Continue to suggestions.

        # 3) CLEAN QUERY & SEARCH AGAIN
        cleaned = _clean_query(text)
        cleaned_results = []
        if cleaned:
            try:
                cleaned_results = await search_series(cleaned)
            except:
                pass

        cleaned_results = [clean_series_name(r) for r in cleaned_results]

        # if cleaned_results still has strict match
        strict2 = _strict_match_only(cleaned, cleaned_results)
        if strict2:
            series = strict2[0]
            return await _send_series_entry(cyber, m, series)

        # 4) Build fuzzy candidate pool
        candidates = list({*results, *cleaned_results})
        if not candidates:
            return await m.reply_text(
                f"❌ No series found `<code>{text}</code>`",
                parse_mode=enums.ParseMode.HTML
            )

        fuzzy = _fuzzy_match(text, candidates, limit=10)
        if not fuzzy:
            return await m.reply_text(
                f"❌ No series found `<code>{text}</code>`",
                parse_mode=enums.ParseMode.HTML
            )

        # Take top 3 unique
        suggestions = []
        for n, score in fuzzy:
            if n not in suggestions:
                suggestions.append(n)
            if len(suggestions) >= 3:
                break

        # Show suggestions
        uid = m.from_user.id if m.from_user else 0
        kb = [[InlineKeyboardButton(s, callback_data=f"sugg_select|{uid}|{s}")]
              for s in suggestions]
        kb.append([InlineKeyboardButton("Close", callback_data=f"close_suggestion|{uid}")])

        msg = await m.reply(
            f"I couldn't find anything related to `<code>{text}</code>`.\nDid you mean any of these?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.HTML
        )

        return

    # -----------------------------------------------------
    # SUGGESTION SELECTION
    # -----------------------------------------------------
    @cyber.on_callback_query(filters.regex("^sugg_select\\|"))
    async def suggestion_select(_, q: CallbackQuery):
        _, uid, series = q.data.split("|", 2)
        await q.answer(f"Selected: {series}")
        return await _edit_to_series_entry(cyber, q, series)

    @cyber.on_callback_query(filters.regex("^close_suggestion\\|"))
    async def suggestion_close(c, q: CallbackQuery):
        try:
            await q.message.delete()
        except:
            pass
        await q.answer()

    # -----------------------------------------------------
    # gss → gls → gqs handlers
    # -----------------------------------------------------
    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q: CallbackQuery):
        _, series, season = q.data.split("|")
        season = int(season)

        langs = await get_unique_languages_by_season(series, season)
        if not langs:
            return await q.answer("No languages found", show_alert=True)

        kb = [
            [InlineKeyboardButton(l, callback_data=f"gls|{series}|{season}|{l}")]
            for l in langs
        ]
        kb.append([InlineKeyboardButton("↺ Back", callback_data=f"gback|{series}")])

        await q.message.edit_text(
            f"📺 **{series} — S{season}**\n\nSelect Language:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await q.answer()

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q: CallbackQuery):
        _, series, season, lang = q.data.split("|")
        season = int(season)

        qualities = await get_unique_qualities_by_lang_season(series, lang, season)
        if not qualities:
            return await q.answer("No qualities found", show_alert=True)

        kb = [
            [InlineKeyboardButton(qy, callback_data=f"gqs|{series}|{season}|{lang}|{qy}")]
            for qy in qualities
        ]
        kb.append([
            InlineKeyboardButton("↺ Back", callback_data=f"gss|{series}|{season}")
        ])

        await q.message.edit_text(
            f"🎬 {series} — S{season} ({lang})\n\nSelect Quality:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await q.answer()

    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q: CallbackQuery):
        _, series, season, lang, quality = q.data.split("|")
        season = int(season)

        bot = await cyber.get_me()
        param = _encode_payload(series, season, lang, quality)
        link = f"https://t.me/{bot.username}?start={param}"

        kb = [[InlineKeyboardButton("🔐 Open Bot (PM)", url=link)]]

        await q.message.edit_text(
            f"✔ **{series} — S{season} ({lang}) [{quality}]**\n\n"
            f"Tap the button to receive files in private chat.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await q.answer("Open bot in PM", show_alert=True)

    @cyber.on_callback_query(filters.regex("^gback\\|"))
    async def gback(_, q: CallbackQuery):
        _, series = q.data.split("|")

        summary = await get_series_summary(series)
        seasons = summary.get("seasons", [])
        kb = [
            [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{series}|{s}")]
            for s in seasons
        ]

        await q.message.edit_text(
            f"**{series}**\n\nSelect Season:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await q.answer()


# ---------------------------------------------------------
# SEND SERIES RESULT TO GROUP (STRICT MATCH only)
# ---------------------------------------------------------
async def _send_series_entry(cyber, m: Message, series: str):
    poster = await _build_poster_for_series(series)
    summary = await get_series_summary(series)
    seasons = summary.get("seasons", [])

    kb = [
        [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{series}|{s}")]
        for s in seasons
    ]

    caption = (
        f"**{series}**\n\n"
        f"Available Seasons: {len(seasons)}\n"
        f"Select a season to continue:"
    )

    try:
        return await m.reply_photo(
            poster,
            caption,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except:
        return await m.reply_text(
            caption,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )


# ---------------------------------------------------------
# EDIT SUGGESTION → SEASON MENU
# ---------------------------------------------------------
async def _edit_to_series_entry(cyber, q: CallbackQuery, series: str):
    summary = await get_series_summary(series)
    seasons = summary.get("seasons", [])

    kb = [
        [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{series}|{s}")]
        for s in seasons
    ]

    txt = (
        f"**Title:** {series}\n"
        f"**Seasons:** {len(seasons)}\n\n"
        f"Select a season:"
    )

    try:
        await q.message.edit_text(
            txt,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except:
        pass

    await q.answer()
