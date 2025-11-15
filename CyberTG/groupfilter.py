# groupfilter.py
"""
Group-based search + season/language/quality selection
Auto-PM sending with typing indicator (no group spam)
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
    get_unique_qualities_by_lang_season,
    get_all_files_by_season_and_quality
)
from utils import clean_series_name, check_force_subscribe
from config import DEFAULT_POSTER, CUSTOM_CAPTION_TEXT, MAX_CALLBACK_LENGTH
from .poster import get_series_poster_async


def register_group_handlers(cyber: Client):

    # -------------------------------------------------------
    # 1. GROUP SEARCH HANDLER
    # -------------------------------------------------------
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

        keyboard = []
        for s in summary.get("seasons", []):
            keyboard.append([
                InlineKeyboardButton(
                    f"Season {s}",
                    callback_data=f"gss|{short}|{s}"
                )
            ])

        await message.reply_photo(
            photo=poster,
            caption=f"🎬 **{series}**\nSelect a season:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # -------------------------------------------------------
    # 2. SEASON → LANGUAGE MENU
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def season_handler(cyber, query: CallbackQuery):
        _, series, season = query.data.split("|")
        season = int(season)

        langs = await get_unique_languages_by_season(series, season)
        if not langs:
            await query.answer("No languages found.", show_alert=True)
            return

        keyboard = []
        for lang in langs:
            keyboard.append([
                InlineKeyboardButton(
                    lang,
                    callback_data=f"gls|{series}|{season}|{lang}"
                )
            ])

        await query.message.edit_caption(
            f"🎬 **{series}** — Season {season}\nSelect Language:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await query.answer()

    # -------------------------------------------------------
    # 3. LANGUAGE → QUALITY MENU
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def language_handler(cyber, query: CallbackQuery):
        _, series, season, lang = query.data.split("|")
        season = int(season)

        qualities = await get_unique_qualities_by_lang_season(series, lang, season)
        if not qualities:
            await query.answer("No qualities found.", show_alert=True)
            return

        keyboard = []
        for q in qualities:
            keyboard.append([
                InlineKeyboardButton(
                    q,
                    callback_data=f"gqs|{series}|{season}|{lang}|{q}"
                )
            ])

        await query.message.edit_caption(
            f"🎬 {series} – S{season} ({lang})\nSelect Quality:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await query.answer()

    # -------------------------------------------------------
    # 4. QUALITY → AUTO-PM SENDING (typing indicator only)
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def quality_handler(cyber, query: CallbackQuery):
        _, series, season, lang, quality = query.data.split("|")
        season = int(season)
        user_id = query.from_user.id

        # No group spam / No popup
        await query.answer()

        # --- Contact user in PM using typing action ---
        try:
            # Typing indicator for natural feel
            await cyber.send_chat_action(user_id, enums.ChatAction.TYPING)
            await asyncio.sleep(1)

            await cyber.send_message(
                user_id,
                f"Preparing your files...\n"
                f"{series} — S{season} • {lang} • {quality}"
            )
        except:
            await query.answer("Open bot PM and press START!", show_alert=True)
            return

        # --- Force Subscribe Check ---
        is_member, invite = await check_force_subscribe(cyber, user_id)
        if not is_member:
            return await cyber.send_message(
                user_id,
                "Join the required channel to receive files.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Join Channel", url=invite)]
                ])
            )

        # --- Fetch all files ---
        files = await get_all_files_by_season_and_quality(series, season, quality, lang)
        if not files:
            return await cyber.send_message(user_id, "No files found.")

        bot_name = (await cyber.get_me()).username

        # --- Send files (with typing indicator before each) ---
        for f in files:
            try:
                await cyber.send_chat_action(user_id, enums.ChatAction.TYPING)
                await asyncio.sleep(0.8)

                file_id = f.get("file_id")
                caption = CUSTOM_CAPTION_TEXT.format(
                    file_name=f.get("file_name"),
                    bot_username=bot_name
                )

                if file_id:
                    await cyber.send_document(user_id, file_id, caption=caption)
                else:
                    await cyber.copy_message(
                        user_id,
                        f["channel_id"],
                        f["message_id"],
                        caption=caption
                    )

                await asyncio.sleep(1)

            except Exception:
                continue

        # --- Final message ---
        await cyber.send_chat_action(user_id, enums.ChatAction.TYPING)
        await asyncio.sleep(1)

        await cyber.send_message(
            user_id,
            f"Done! All episodes of {series} S{season} ({quality}) have been sent."
        )

    return [
        group_search_handler,
        season_handler,
        language_handler,
        quality_handler
    ]
