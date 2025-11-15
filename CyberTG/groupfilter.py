# groupfilter.py
"""
Group-based search → Season → Language → Quality
When user clicks Quality, bot gives a deep-link button to open DM and auto-start sending files.

Deep-link format (Option B2 - Base64 of pipe-separated, percent-encoded tuple):
    send_<BASE64_URLSAFE>

Payload before encoding:
    "{series_quoted}|{season}|{lang_quoted}|{quality_quoted}"

Where values are percent-encoded using urllib.parse.quote to avoid delimiter collisions.
"""
import asyncio
import base64
import urllib.parse

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


def _make_send_payload_b64(series: str, season: int, lang: str, quality: str) -> str:
    """
    Build the Base64 (urlsafe) payload for deep-link.
    Uses percent-encoding for each field, joins with '|' and encodes.
    """
    # Percent-encode values to avoid '|' collisions
    s_enc = urllib.parse.quote(series, safe="")
    lang_enc = urllib.parse.quote(lang, safe="")
    quality_enc = urllib.parse.quote(quality, safe="")

    raw = f"{s_enc}|{season}|{lang_enc}|{quality_enc}"
    b64 = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    # Keep padding intact (Telegram supports '=' in URLs). If you prefer shorter links, you may strip '=' but then add padding back before decoding.
    return b64


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
    # QUALITY → DEEP LINK TO PM (Base64 payload)
    # ------------------------------
    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def gqs_handler(cyber, query: CallbackQuery):
        _, series, season, lang, quality = query.data.split("|")
        season = int(season)

        bot = await cyber.get_me()
        b64_payload = _make_send_payload_b64(series, season, lang, quality)
        deep_link = f"https://t.me/{bot.username}?start=send_{b64_payload}"

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📩 Open Bot & Start Download", url=deep_link)]]
        )

        # Edit message to show DM button
        await query.message.edit_caption(
            f"📥 Tap below to receive files in DM.\n\n"
            f"🎬 {series}\n"
            f"Season {season} • {lang} • {quality}",
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        await query.answer()

    return []
