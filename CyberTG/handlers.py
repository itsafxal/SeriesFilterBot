# groupfilter.py
"""
Group-based search + filter + auto-PM-send system
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

        # search
        results = await search_series(query)
        if not results:
            return

        series = clean_series_name(results[0])
        summary = await get_series_summary(series)

        poster = await get_series_poster_async(series) or DEFAULT_POSTER

        # Build season buttons
        keyboard = []
        short = series[:MAX_CALLBACK_LENGTH]

        for s in summary.get("seasons", []):
            keyboard.append([
                InlineKeyboardButton(
                    f"Season {s}",
                    callback_data=f"gss|{short}|{s}"
                )
            ])

        await message.reply_photo(
            photo=poster,
            caption=f"🎬 **{series}**\n"
                    f"Select a season:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # -------------------------------------------------------
    # 2. SEASON → LANGUAGE MENU
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def group_season_handler(cyber, query: CallbackQuery):
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
            f"🎬 **{series}** — Season {season}\n"
            f"Select Language:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await query.answer()

    # -------------------------------------------------------
    # 3. LANGUAGE → QUALITY MENU
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def group_language_handler(cyber, query: CallbackQuery):
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
            f"🎬 {series} – S{season} ({lang})\n"
            f"Select Quality:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await query.answer()

    # -------------------------------------------------------
    # 4. QUALITY → AUTO-PM → FORCE-SUB → SEND FILES + NOTIFY
    # -------------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def group_quality_handler(cyber, query: CallbackQuery):
        _, series, season, lang, quality = query.data.split("|")
        season = int(season)

        user_id = query.from_user.id

        # *** GROUP NOTIFICATION ***
        await query.message.reply_text(
            f"📩 <b>{query.from_user.first_name}</b>, please check your DM.\n"
            f"The bot is sending:\n"
            f"🎬 <b>{series}</b>\n"
            f"📀 Season <b>{season}</b> • {lang} • {quality}",
            parse_mode=enums.ParseMode.HTML
        )

        # *** SEND START MESSAGE TO USER PM ***
        try:
            await cyber.send_message(
                user_id,
                f"⏳ Preparing your files...\n\n"
                f"🎬 <b>{series}</b>\n"
                f"📀 Season <b>{season}</b>\n"
                f"🌐 {lang} • {quality}",
                parse_mode=enums.ParseMode.HTML
            )
        except:
            await query.answer("Open bot PM and press START!", show_alert=True)
            return

        await query.answer("Check your PM", cache_time=0)

        # *** FORCE SUBSCRIBE CHECK ***
        is_member, invite = await check_force_subscribe(cyber, user_id)
        if not is_member:
            await cyber.send_message(
                user_id,
                "⚠️ You must join our channel to receive the files.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Join Channel", url=invite)]
                ])
            )
            return

        # *** FETCH FILES ***
        files = await get_all_files_by_season_and_quality(series, season, quality, lang)
        if not files:
            await cyber.send_message(user_id, "❌ No files found.")
            return

        bot_name = (await cyber.get_me()).username

        # *** NOTIFY BEFORE SENDING ***
        await cyber.send_message(
            user_id,
            f"📥 Sending all episodes now...\n"
            f"🎬 <b>{series}</b> — Season {season}",
            parse_mode=enums.ParseMode.HTML
        )

        # *** SEND FILES ***
        for f in files:
            file_id = f.get("file_id")
            caption = CUSTOM_CAPTION_TEXT.format(
                file_name=f.get("file_name"),
                bot_username=bot_name
            )

            try:
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

        # *** FINAL MESSAGE ***
        await cyber.send_message(
            user_id,
            f"✅ Completed sending **{series} — S{season} ({quality})**",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    return [
        group_search_handler,
        group_season_handler,
        group_language_handler,
        group_quality_handler
    ]
