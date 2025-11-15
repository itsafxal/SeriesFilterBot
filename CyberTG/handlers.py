# handlers.py (FULL FIXED VERSION)
# Includes deep-link fix, safe decoding, no truncation, file sending works

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
    get_all_files_by_season_and_quality,
    get_series_summary,
    get_global_setting,
    get_file_by_message_id_and_channel_id,
)

from config import (
    logger,
    CUSTOM_CAPTION_TEXT,
    DEFAULT_POSTER,
    ADMIN_USERS,
    OMDB_CACHE_DB,
    MAX_CALLBACK_LENGTH,
)

from utils import (
    clean_series_name,
    delete_message_later,
    check_force_subscribe,
)

from .poster import get_series_poster_async
from .menus import (
    show_language_menu, show_quality_menu, help_menu_handler, cyber_menu_handler,
    admin_menu_handler, about_menu_handler, close_menu_handler,
    back_to_language_handler, back_to_search_handler,
)

# IMPORTANT — for decoding deep-link arguments
from .groupfilter import safe_decode


# -------------------------------------------------------------------
#   FILE DELIVERY LOGIC
# -------------------------------------------------------------------
async def send_files_to_user(cyber: Client, user_id: int, series_name: str, season: int,
                             quality: str, language: str):

    # Force-sub check
    is_member, invite_link = await check_force_subscribe(cyber, user_id)
    if not is_member:
        await cyber.send_message(
            chat_id=user_id,
            text="🛑 Please join our channel to get the files.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Join Channel", url=invite_link)]]
            ),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        return

    # Fetch files
    files = await get_all_files_by_season_and_quality(
        series_name, season, quality, language
    )

    if not files:
        await cyber.send_message(
            chat_id=user_id,
            text=f"❌ No files found for {series_name} S{season} ({quality}/{language}).",
            parse_mode=enums.ParseMode.MARKDOWN
        )
        return

    bot_username = (await cyber.get_me()).username
    await cyber.send_message(
        chat_id=user_id,
        text=f"Sending {series_name} S{season} ({quality}/{language}). Please wait...",
        parse_mode=enums.ParseMode.MARKDOWN
    )

    sent_count = 0

    for entry in files:
        channel_id = entry["channel_id"]
        message_id = entry["message_id"]
        file_name = entry.get("file_name", "Episode")

        custom_caption = CUSTOM_CAPTION_TEXT.format(
            file_name=file_name,
            bot_username=bot_username
        )

        try:
            file_id = entry.get("file_id")
            if file_id:
                await cyber.send_document(
                    chat_id=user_id,
                    document=file_id,
                    caption=custom_caption,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            else:
                await cyber.copy_message(
                    chat_id=user_id,
                    from_chat_id=channel_id,
                    message_id=message_id,
                    caption=custom_caption,
                    parse_mode=enums.ParseMode.MARKDOWN
                )

            sent_count += 1
            await asyncio.sleep(1.5)

        except Exception as e:
            logger.error(f"Send failed for msg {message_id}: {e}")
            continue

    await cyber.send_message(
        chat_id=user_id,
        text=f"✅ Sent {sent_count} files from {series_name} — S{season} {quality}",
        parse_mode=enums.ParseMode.MARKDOWN
    )


# -------------------------------------------------------------------
#   HANDLER REGISTRATION
# -------------------------------------------------------------------
def register_conversation_handlers(cyber: Client):

    # -----------------------------
    #   PRIVATE SEARCH HANDLER
    # -----------------------------
    @cyber.on_message(filters.text & filters.private & ~filters.command(["start", "imdb", "autodel", "forcesub"]))
    async def search_handler(cyber, message: Message):

        query = message.text.strip()
        if query.startswith("/") and query.lower() not in ["/index"]:
            return

        is_member, invite_link = await check_force_subscribe(cyber, message.from_user.id)
        if not is_member:
            await message.reply_text(
                "🛑 Please join our channel to search.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Join Channel", url=invite_link)]]
                ),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            return

        try:
            names = await search_series(query)
        except:
            names = []

        unique = list(dict.fromkeys([clean_series_name(x) for x in names]))
        if not unique:
            return await message.reply("❌ No results found.")

        if len(unique) > 1:
            user_id = message.from_user.id
            top = unique[:5]
            kb = [
                [InlineKeyboardButton(m, callback_data=f"sugg_select|{user_id}|{m}")]
                for m in top
            ]
            kb.append([InlineKeyboardButton("Close", callback_data=f"close_suggestion|{user_id}")])

            return await message.reply(
                f"I found {len(unique)} results for {query}. Select:",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        series = unique[0]
        await show_single_series(cyber, message, series)

    # -----------------------------
    #   FIXED /start DEEP LINK HANDLER
    # -----------------------------
    @cyber.on_message(filters.command("start") & filters.private)
    async def deep_link_start_handler(cyber, message: Message):

        if len(message.command) > 1:
            payload = message.command[1]

            if payload.startswith("file_request|"):
                parts = payload.split("|")

                if len(parts) == 5:

                    # ---- FIXED DECODING ----
                    series_name = safe_decode(parts[1])

                    try:
                        season = int(parts[2])
                    except ValueError:
                        return await message.reply_text("❌ Invalid season number.")

                    language = safe_decode(parts[3])
                    quality = safe_decode(parts[4])
                    # -------------------------

                    user_id = message.from_user.id

                    await send_files_to_user(
                        cyber,
                        user_id,
                        series_name,
                        season,
                        quality,
                        language
                    )
                    return

        # Default welcome message
        user = message.from_user
        bot = await cyber.get_me()

        START = (
            f"Hello {user.first_name}, welcome to {bot.first_name}.\n"
            "Send me a series name to search."
        )

        await message.reply(START)

    # -----------------------------
    #   SERIES SUGGESTION HANDLER
    # -----------------------------
    @cyber.on_callback_query(filters.regex(r"^sugg_select\|"))
    async def sugg_select_handler(cyber, query: CallbackQuery):
        _, _, name = query.data.split("|", 2)
        await query.answer(f"Selected {name}")
        await show_single_series(cyber, query.message, name)

    @cyber.on_callback_query(filters.regex(r"^close_suggestion\|"))
    async def close_suggestion_handler(cyber, query: CallbackQuery):
        try:
            await cyber.delete_messages(query.message.chat.id, query.message.id)
        except:
            pass
        await query.answer("Closed")

    # -----------------------------
    #   SEASON/LANGUAGE/QUALITY
    # -----------------------------
    @cyber.on_callback_query(filters.regex(r"^ss\|"))
    async def season_select_handler(cyber, query: CallbackQuery):
        _, series, season = query.data.split("|")
        await show_language_menu(cyber, query, series, int(season))
        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^ls\|"))
    async def language_select_handler(cyber, query: CallbackQuery):
        _, series, season, lang = query.data.split("|")
        await show_quality_menu(cyber, query, series, lang, int(season))
        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^qs\|"))
    async def quality_select_handler(cyber, query: CallbackQuery):
        _, series, season, lang, quality = query.data.split("|")
        user_id = query.from_user.id
        await query.answer("Sending...")

        await send_files_to_user(
            cyber,
            user_id,
            series,
            int(season),
            quality,
            lang
        )

    # -----------------------------
    #   SINGLE FILE HANDLER
    # -----------------------------
    @cyber.on_callback_query(filters.regex(r"^fs\|"))
    async def file_select_handler(cyber, query: CallbackQuery):
        _, channel, msg = query.data.split("|")

        try:
            channel = int(channel)
            msg = int(msg)
        except:
            return await query.answer("Invalid file link.", show_alert=True)

        entry = await get_file_by_message_id_and_channel_id(channel, msg)
        if not entry:
            return await query.answer("File not found.")

        file_name = entry.get("file_name", "Episode")
        bot_user = (await cyber.get_me()).username

        caption = CUSTOM_CAPTION_TEXT.format(
            file_name=file_name,
            bot_username=bot_user
        )

        try:
            await cyber.copy_message(
                chat_id=query.from_user.id,
                from_chat_id=channel,
                message_id=msg,
                caption=caption,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(e)
            return await query.answer("Failed to send file.", show_alert=True)

        await query.answer("Sent!")

    # -----------------------------
    #   MENU HANDLERS
    # -----------------------------
    @cyber.on_callback_query(filters.regex(r"^help\|"))
    async def help_proxy(cyber, query): return await help_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^cyber\|"))
    async def cyber_proxy(cyber, query): return await cyber_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^admin_cmd\|"))
    async def admin_proxy(cyber, query): return await admin_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^about\|"))
    async def about_proxy(cyber, query): return await about_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"close"))
    async def close_proxy(cyber, query): return await close_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^bl\|"))
    async def back_lang_proxy(cyber, query): return await back_to_language_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^back_to_search\|"))
    async def back_search_proxy(cyber, query): return await back_to_search_handler(cyber, query)

    return [
        search_handler,
        deep_link_start_handler,
        sugg_select_handler,
        close_suggestion_handler,
        season_select_handler,
        language_select_handler,
        quality_select_handler,
        file_select_handler,
    ]


# -------------------------------------------------------------------
#   SHOW SINGLE SERIES (PRIVATE)
# -------------------------------------------------------------------
async def show_single_series(cyber, message, series_name):

    poster = DEFAULT_POSTER
    imdb_enabled = True

    try:
        imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
    except:
        pass

    try:
        with shelve.open(OMDB_CACHE_DB) as db:
            key = f"poster::{series_name.lower()}"
            if key in db:
                poster = db[key]
    except:
        pass

    if poster == DEFAULT_POSTER and imdb_enabled:
        try:
            p = await get_series_poster_async(series_name)
            poster = p or DEFAULT_POSTER
        except:
            pass

    summary = await get_series_summary(series_name)

    caption = (
        f"**Title:** {series_name}\n"
        f"**Languages:** {', '.join(summary.get('languages', []))}\n"
        f"**Seasons:** {len(summary.get('seasons', []))}\n"
        f"**Qualities:** {', '.join(summary.get('qualities', []))}\n\n"
        f"Select a season below:"
    )

    kb = []
    for s in summary.get("seasons", []):
        kb.append([InlineKeyboardButton(f"Season {s}", callback_data=f"ss|{series_name}|{s}")])

    try:
        await message.reply_photo(
            photo=poster,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(kb)
        )
    except:
        await message.reply(
            caption,
            reply_markup=InlineKeyboardMarkup(kb)
        )

