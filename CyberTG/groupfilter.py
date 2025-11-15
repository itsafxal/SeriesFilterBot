# groupfilter.py
import asyncio
import base64
from urllib.parse import quote, unquote
import shelve

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import MessageNotModified

from config import (
    logger,
    DEFAULT_POSTER,
    MAX_CALLBACK_LENGTH,
    OMDB_CACHE_DB
)

from utils import clean_series_name
from utils import check_force_subscribe

from DB.database import (
    search_series,
    get_series_summary,
)

from .poster import get_series_poster_async


# ---------------------------------------------------------
# Poster Builder
# ---------------------------------------------------------
async def _build_poster_for_series(series_name: str):
    poster_url = DEFAULT_POSTER

    try:
        cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
        with shelve.open(cache_file) as cache:
            key = f"poster::{series_name.strip().lower()}"
            if key in cache:
                poster_url = cache[key]
    except:
        pass

    try:
        poster_tmp = await get_series_poster_async(series_name)
        poster_url = poster_tmp or DEFAULT_POSTER
    except Exception as e:
        logger.warning(f"[POSTER FAIL] {series_name}: {e}")
        poster_url = DEFAULT_POSTER

    return poster_url


# ---------------------------------------------------------
# Encode deep-link payload
# ---------------------------------------------------------
def _encode_start_payload(series: str, season: int, language: str, quality: str) -> str:
    raw = f"{series}|{season}|{language}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"send_{quote(b64)}"


# ---------------------------------------------------------
# Register group filter handlers
# ---------------------------------------------------------
def register_groupfilter_handlers(cyber: Client):

    # ======================================================
    # GROUP SEARCH
    # ======================================================
    @cyber.on_message(filters.text & filters.group)
    async def group_search_handler(_, message: Message):

        text = (message.text or "").strip()
        if not text:
            return

        # ignore commands + links
        if (
            text.startswith("/")
            or "http://" in text
            or "https://" in text
        ):
            return

        # Search DB
        try:
            matches = await search_series(text)
        except Exception as e:
            logger.error(f"[GROUP SEARCH ERR] {e}")
            matches = []

        if not matches:
            return

        series = clean_series_name(matches[0])

        # Poster
        poster = await _build_poster_for_series(series)

        # Summary
        try:
            summary = await get_series_summary(series)
            seasons = summary.get("seasons", [])
        except:
            seasons = []

        if not seasons:
            return

        series_short = series[:MAX_CALLBACK_LENGTH]
        keyboard = []

        for s in seasons:
            keyboard.append([
                InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{series_short}|{s}")
            ])

        caption = (
            f"**{series}**\n\n"
            f"Available Seasons: {len(seasons)}\n\n"
            f"Select a season:"
        )

        try:
            await message.reply_photo(
                poster,
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            await message.reply_text(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )

    # ======================================================
    # SEASON SELECT (gss|)
    # ======================================================
    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def gss_handler(_, query: CallbackQuery):

        try:
            _, series, season = query.data.split("|")
            season = int(season)
        except:
            await query.answer("Invalid data", show_alert=True)
            return

        try:
            summary = await get_series_summary(series)
            languages = summary.get("languages", [])
        except:
            languages = []

        if not languages:
            await query.answer("No languages found", show_alert=True)
            return

        series_short = series[:MAX_CALLBACK_LENGTH]
        keyboard = []

        for lang in languages:
            keyboard.append([
                InlineKeyboardButton(lang, callback_data=f"gls|{series_short}|{season}|{lang}")
            ])

        keyboard.append([
            InlineKeyboardButton("↺ Back", callback_data=f"gback|{series_short}")
        ])

        caption = f"🎬 **{series} — S{season}**\n\nSelect Language:"

        try:
            await query.message.edit_caption(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            pass

        await query.answer()

    # ======================================================
    # LANGUAGE SELECT (gls|)
    # ======================================================
    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def gls_handler(_, query: CallbackQuery):

        try:
            _, series, season, language = query.data.split("|")
            season = int(season)
        except:
            await query.answer("Invalid data", show_alert=True)
            return

        # Fetch qualities from summary
        try:
            summary = await get_series_summary(series)
            qualities = summary.get("qualities", [])
        except:
            qualities = []

        if not qualities:
            await query.answer("No qualities found", show_alert=True)
            return

        series_short = series[:MAX_CALLBACK_LENGTH]
        keyboard = []

        for q in qualities:
            keyboard.append([
                InlineKeyboardButton(q, callback_data=f"gqs|{series_short}|{season}|{language}|{q}")
            ])

        keyboard.append([
            InlineKeyboardButton("↺ Back", callback_data=f"gss|{series_short}|{season}")
        ])

        caption = (
            f"📺 **{series} — S{season} ({language})**\n\n"
            f"Select Quality:"
        )

        try:
            await query.message.edit_caption(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            pass

        await query.answer()

    # ======================================================
    # QUALITY SELECT — ONLY SHOW PM LINK (gqs|)
    # ======================================================
    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def gqs_handler(cyber, query: CallbackQuery):

        try:
            _, series, season, language, quality = query.data.split("|")
            season = int(season)
        except:
            await query.answer("Invalid data", show_alert=True)
            return

        # Create deep link
        param = _encode_start_payload(series, season, language, quality)
        bot = await cyber.get_me()
        deeplink = f"https://t.me/{bot.username}?start={param}"

        keyboard = [
            [InlineKeyboardButton("🔐 Open Bot (PM)", url=deeplink)]
        ]

        caption = (
            f"✔ **{series} — S{season} ({language}) [{quality}]**\n\n"
            f"Tap 'Open Bot' to receive files in private chat."
        )

        try:
            await query.message.edit_caption(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            pass

        await query.answer("Open bot in PM", show_alert=True)

    # ======================================================
    # BACK BUTTON (gback|)
    # ======================================================
    @cyber.on_callback_query(filters.regex(r"^gback\|"))
    async def gback_handler(_, query: CallbackQuery):

        try:
            _, series = query.data.split("|")
        except:
            await query.answer()
            return

        try:
            summary = await get_series_summary(series)
            seasons = summary.get("seasons", [])
        except:
            seasons = []

        if not seasons:
            await query.answer("No seasons found", show_alert=True)
            return

        series_short = series[:MAX_CALLBACK_LENGTH]
        keyboard = []

        for s in seasons:
            keyboard.append([
                InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{series_short}|{s}")
            ])

        caption = f"**{series}**\n\nSelect Season:"

        try:
            await query.message.edit_caption(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            pass

        await query.answer()

    # end register()
