# groupfilter.py
"""
Group-based search → Season → Language → Quality → Redirect to PM

IMPORTANT:
- This version uses the EXACT SAME normalizer as SQLITE (_normalized_series_from_filename)
- NO series name truncation (MAX_CALLBACK_LENGTH removed)
- Deep-link payload is fully correct for DB lookup
"""

import asyncio
import base64
import urllib.parse

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)

# ---- IMPORT DB HELPERS ----
from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season
)

# USE SQLITE NORMALIZER to match DB storage EXACTLY
from DB.sqlite_db import _normalized_series_from_filename

from config import DEFAULT_POSTER
from .poster import get_series_poster_async



# -----------------------------------------------------------
# Build Base64 deep-link payload
# -----------------------------------------------------------
def _make_send_payload_b64(series: str, season: int, lang: str, quality: str) -> str:
    s_enc = urllib.parse.quote(series, safe="")
    lang_enc = urllib.parse.quote(lang, safe="")
    quality_enc = urllib.parse.quote(quality, safe="")

    raw = f"{s_enc}|{season}|{lang_enc}|{quality_enc}"
    b64 = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return b64



# -----------------------------------------------------------
# REGISTER GROUP HANDLERS
# -----------------------------------------------------------
def register_group_handlers(cyber: Client):

    # -------------------------------------------------------
    # GROUP SEARCH HANDLER
    # -------------------------------------------------------
    @cyber.on_message(filters.group & filters.text)
    async def group_search_handler(cyber, message: Message):
        query = message.text.strip()
        if query.startswith("/"):
            return

        results = await search_series(query)
        if not results:
            return

        # Normalize series name EXACTLY like SQLite
        series = _normalized_series_from_filename(results[0])

        summary = await get_series_summary(series)
        poster = await get_series_poster_async(series) or DEFAULT_POSTER

        keyboard = [
            [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{series}|{s}")]
            for s in summary.get("seasons", [])
        ]

        await message.reply_photo(
            poster,
            caption=f"🎬 **{series}**\nSelect Season:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )



    # -------------------------------------------------------
    # SEASON → LANGUAGE
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def gss_handler(cyber, query: CallbackQuery):
        _, series, season = query.data.split("|", 2)
        season = int(season)

        langs = await get_unique_languages_by_season(series, season)
        if not langs:
            return await query.answer("No languages available.", show_alert=True)

        keyboard = [
            [InlineKeyboardButton(lang, callback_data=f"gls|{series}|{season}|{lang}")]
            for lang in langs
        ]

        await query.message.edit_caption(
            f"🎬 {series} — Season {season}\nSelect Language:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        await query.answer()



    # -------------------------------------------------------
    # LANGUAGE → QUALITY
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def gls_handler(cyber, query: CallbackQuery):
        _, series, season, lang = query.data.split("|", 3)
        season = int(season)

        qualities = await get_unique_qualities_by_lang_season(series, lang, season)
        if not qualities:
            return await query.answer("No qualities available.", show_alert=True)

        keyboard = [
            [InlineKeyboardButton(q, callback_data=f"gqs|{series}|{season}|{lang}|{q}")]
            for q in qualities
        ]

        await query.message.edit_caption(
            f"🎬 {series} — S{season} ({lang})\nSelect Quality:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        await query.answer()



    # -------------------------------------------------------
    # QUALITY → DEEP LINK TO PM (Auto-send)
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def gqs_handler(cyber, query: CallbackQuery):
        _, series, season, lang, quality = query.data.split("|", 4)
        season = int(season)

        bot = await cyber.get_me()

        # Build encoded payload
        b64_payload = _make_send_payload_b64(series, season, lang, quality)

        # Deep-link format
        deep_link = f"https://t.me/{bot.username}?start=send_{b64_payload}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Open Bot & Start Download", url=deep_link)]
        ])

        await query.message.edit_caption(
            f"📥 Tap below to receive files in DM.\n\n"
            f"🎬 {series}\n"
            f"Season {season} • {lang} • {quality}",
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        await query.answer()

    return []
