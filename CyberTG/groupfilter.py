# groupfilter.py
"""
Group-based search → Season → Language → Quality
When user clicks Quality, bot gives a deep-link button to open DM and auto-start sending files.
"""

import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season
)
from utils import clean_series_name
from config import DEFAULT_POSTER, MAX_CALLBACK_LENGTH
from .poster import get_series_poster_async


def register_group_handlers(cyber: Client):

    # ------------------------------
    # GROUP SEARCH
    # ------------------------------
    @cyber.on_message(filters.group & filters.text)
    async def group_search_handler(cyber, message: Message):
        query = message.text.strip()
        if query.startswith("/"):
            return

        results = await search_series(query)
        if not results:
            return

        series = clean_series_name(results[0])
        summary = await get_series_summary(series)
        poster = await get_series_poster_async(series) or DEFAULT_POSTER

        short = series[:MAX_CALLBACK_LENGTH]

        keyboard = [
            [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{short}|{s}")]
            for s in summary.get("seasons", [])
        ]

        await message.reply_photo(
            poster,
            caption=f"🎬 **{series}**\nSelect Season:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # ------------------------------
    # SEASON → LANGUAGE
    # ------------------------------
    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def gss_handler(cyber, query: CallbackQuery):
        _, series, season = query.data.split("|")
        season = int(season)

        langs = await get_unique_languages_by_season(series, season)
        if not langs:
            return await query.answer("No languages", show_alert=True)

        keyboard = [
            [InlineKeyboardButton(lang, callback_data=f"gls|{series}|{season}|{lang}")]
            for lang in langs
        ]

        await query.message.edit_caption(
            f"🎬 {series} — Season {season}\nSelect Language:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await query.answer()

    # ------------------------------
    # LANGUAGE → QUALITY
    # ------------------------------
    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def gls_handler(cyber, query: CallbackQuery):
        _, series, season, lang = query.data.split("|")
        season = int(season)

        qualities = await get_unique_qualities_by_lang_season(series, lang, season)

        keyboard = [
            [InlineKeyboardButton(q, callback_data=f"gqs|{series}|{season}|{lang}|{q}")]
            for q in qualities
        ]

        await query.message.edit_caption(
            f"🎬 {series} — S{season} ({lang})\nSelect Quality:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await query.answer()

    # ------------------------------
    # QUALITY → DEEP LINK TO PM
    # ------------------------------
    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def gqs_handler(cyber, query: CallbackQuery):
        _, series, season, lang, quality = query.data.split("|")
        season = int(season)

        bot = await cyber.get_me()
        deep_link = f"https://t.me/{bot.username}?start=send_{series}_{season}_{lang}_{quality}"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📩 Open Bot & Start Download", url=deep_link)]]
        )

        # Edit message to show DM button (not annoying)
        await query.message.edit_caption(
            f"📥 Tap below to receive files in DM.\n\n"
            f"🎬 {series}\n"
            f"Season {season} • {lang} • {quality}",
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        await query.answer()

    return []
