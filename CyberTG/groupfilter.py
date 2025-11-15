# groupfilter.py (Corrected Full Version)
# FIXED: Series name truncation, safe callback encoding, deep-link consistency, full name preservation

import asyncio
import re
import shelve
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    CallbackQuery,
)
from pyrogram.errors import MessageNotModified

from DB.database import (
    search_series,
    get_series_summary,
    get_global_setting,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)
from config import (
    logger,
    DEFAULT_POSTER,
    OMDB_CACHE_DB,
    MAX_CALLBACK_LENGTH,
)
from utils import (
    clean_series_name,
    delete_message_later,
)
from .poster import get_series_poster_async


# Utility for safe callback

def safe_encode(text: str) -> str:
    return text.replace("|", "_pipe_").replace(" ", "_space_")

def safe_decode(text: str) -> str:
    return text.replace("_pipe_", "|").replace("_space_", " ")


# --- GROUP HANDLERS ---

def register_group_handlers(cyber: Client):

    @cyber.on_message(filters.text & filters.group & ~filters.command(["start", "imdb"]))
    async def group_search_handler(cyber, message: Message):
        query = message.text.strip()
        if query.startswith("/") and query.lower() not in ["/index"]:
            return

        clean_query = re.sub(
            r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\\sme)?)|movie(s)?|new|latest|br((o|u)h?)*|^h(e|a)?(l)*(o)*|mal(ayalam)?|t(h)?amil|file|that|find|und(o)*|kit(t(i|y)?)?o(w)?|thar(u)?(o)*w?|kittum(o)*|aya(k)*(um(o)*)?|full\\smovie|any(one)|with\\ssubtitle(s)?)",
            "", message.text, flags=re.IGNORECASE).strip()

        search_term = clean_query or query
        if not search_term:
            return

        try:
            all_found = await search_series(search_term)
        except Exception:
            all_found = []

        unique_names = list(dict.fromkeys([clean_series_name(n) for n in all_found]))
        if not unique_names:
            return

        # Multiple results → suggestions
        if len(unique_names) > 1:
            top5 = unique_names[:5]
            user_id = message.from_user.id

            kb = [[
                InlineKeyboardButton(
                    movie,
                    callback_data=f"gsugg_select|{user_id}|{safe_encode(movie)}"
                )
            ] for movie in top5]
            kb.append([InlineKeyboardButton("Close", callback_data=f"close_suggestion|{user_id}")])

            sent = await message.reply(
                f"🎬 Found {len(unique_names)} results for **{query}**. Select below:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            asyncio.create_task(delete_message_later(cyber, sent.chat.id, sent.id, 60))
            return

        # Single result
        await display_series_menu(cyber, message, unique_names[0], is_callback=False)


    @cyber.on_callback_query(filters.regex(r"^gsugg_select\|"))
    async def group_sugg_select_handler(cyber, query: CallbackQuery):
        _, _, encoded_name = query.data.split("|", 2)
        series_name = safe_decode(encoded_name)
        await query.answer(f"Selected: {series_name}")
        await display_series_menu(cyber, query.message, series_name, True, query)


    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def group_season_select(cyber, query: CallbackQuery):
        _, encoded_series, season = query.data.split("|")
        series_name = safe_decode(encoded_series)
        await show_group_language_menu(cyber, query, series_name, int(season))


    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def group_lang_select(cyber, query: CallbackQuery):
        _, encoded_series, season, lang = query.data.split("|")
        series_name = safe_decode(encoded_series)
        await show_group_quality_menu(cyber, query, series_name, safe_decode(lang), int(season))


    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def group_quality_select(cyber, query: CallbackQuery):
        _, encoded_series, season, lang, quality = query.data.split("|")
        series_name = safe_decode(encoded_series)
        lang = safe_decode(lang)
        quality = safe_decode(quality)

        bot_username = (await cyber.get_me()).username
        payload = f"file_request|{safe_encode(series_name)}|{season}|{safe_encode(lang)}|{safe_encode(quality)}"
        deeplink = f"https://t.me/{bot_username}?start={payload}"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📥 Get S{season} ({quality}/{lang}) in PM",
                url=deeplink
            )]
        ])

        caption = (
            f"✅ Redirecting to PM for files\n\n"
            f"Go to bot chat and press Start if needed.\n\n"
            f"**{series_name}** S{season}\n"
            f"Quality: **{quality}**, Language: **{lang}**"
        )

        try:
            await query.message.edit_caption(caption, reply_markup=kb)
        except Exception:
            await query.message.edit_text(caption, reply_markup=kb)

        await query.answer("Open PM to get files")


    # ---------- DISPLAY FUNCTIONS ----------

    async def display_series_menu(cyber, message: Message, series_name: str, is_callback: bool,
                                  callback_query: CallbackQuery = None):

        poster = DEFAULT_POSTER
        imdb_enabled = True
        try:
            imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
        except Exception:
            pass

        # cache
        try:
            with shelve.open(OMDB_CACHE_DB) as db:
                key = f"poster::{series_name.lower()}"
                if key in db:
                    poster = db[key]
        except Exception:
            pass

        if poster == DEFAULT_POSTER and imdb_enabled:
            try:
                newposter = await get_series_poster_async(series_name)
                poster = newposter or DEFAULT_POSTER
            except Exception:
                pass

        summary = await get_series_summary(series_name)

        caption = (
            f"**○ Title:** {series_name}\n"
            f"**○ Languages:** {', '.join(summary.get('languages', []))}\n"
            f"**○ Seasons:** {len(summary.get('seasons', []))}\n"
            f"**○ Qualities:** {', '.join(summary.get('qualities', []))}\n\n"
            f"Select a season below:"
        )

        kb = []
        encoded_series = safe_encode(series_name)
        for s in summary.get("seasons", []):
            kb.append([InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{encoded_series}|{s}")])

        markup = InlineKeyboardMarkup(kb)

        if is_callback:
            try:
                await callback_query.message.edit_media(
                    InputMediaPhoto(poster, caption=caption), reply_markup=markup
                )
            except Exception:
                await callback_query.message.edit_text(caption, reply_markup=markup)
        else:
            try:
                sent = await message.reply_photo(poster, caption=caption, reply_markup=markup)
            except Exception:
                sent = await message.reply_text(caption, reply_markup=markup)
            asyncio.create_task(delete_message_later(cyber, sent.chat.id, sent.id, 60))


    async def show_group_language_menu(cyber, query, series_name, season: int):
        langs = await get_unique_languages_by_season(series_name, season)
        if not langs:
            await query.answer("No languages found", show_alert=True)
            return

        kb = []
        encoded_series = safe_encode(series_name)
        for lang in langs:
            kb.append([
                InlineKeyboardButton(f"{lang}", callback_data=f"gls|{encoded_series}|{season}|{safe_encode(lang)}")
            ])

        kb.append([InlineKeyboardButton("↺ Back", callback_data=f"gss|{encoded_series}|{season}")])

        caption = f"Select language for {series_name} S{season}"
        try:
            await query.message.edit_caption(caption, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(kb))


    async def show_group_quality_menu(cyber, query, series_name, lang, season: int):
        qualities = await get_unique_qualities_by_lang_season(series_name, lang, season)
        if not qualities:
            await query.answer("No qualities found", show_alert=True)
            return

        kb = []
        encoded_series = safe_encode(series_name)
        encoded_lang = safe_encode(lang)

        for q in qualities:
            kb.append([
                InlineKeyboardButton(
                    q, callback_data=f"gqs|{encoded_series}|{season}|{encoded_lang}|{safe_encode(q)}"
                )
            ])

        kb.append([
            InlineKeyboardButton(
                "↺ Back", callback_data=f"gls|{encoded_series}|{season}|{encoded_lang}"
            )
        ])

        caption = f"Select quality for {series_name} S{season} ({lang})"
        try:
            await query.message.edit_caption(caption, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(kb))


    return [
        group_search_handler,
        group_sugg_select_handler,
        group_season_select,
        group_lang_select,
        group_quality_select,
    ]
