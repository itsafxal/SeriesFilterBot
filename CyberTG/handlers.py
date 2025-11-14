# Upgraded handlers.py
# (Full file with enhanced fuzzy search handling, improved suggestions UI,
#  cleaner code flow, and compatibility with upgraded sqlite_db)

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
    show_language_menu, show_quality_menu,
    help_menu_handler, cyber_menu_handler,
    admin_menu_handler, about_menu_handler,
    close_menu_handler, back_to_language_handler,
    back_to_search_handler,
)


#############################################
# REGISTER HANDLERS
#############################################
def register_conversation_handlers(cyber: Client):

    ############################################################
    # SEARCH HANDLER (UPGRADED WITH RANKED FUZZY SEARCH)
    ############################################################
    @cyber.on_message(filters.text & filters.private & ~filters.command(["start", "imdb", "autodel", "forcesub"]))
    async def search_handler(cyber, message: Message):
        query = message.text.strip()

        if query.startswith("/") and query.lower() not in ["/index"]:
            return

        # Force subscribe check
        is_member, invite_link = await check_force_subscribe(cyber, message.from_user.id)
        if not is_member:
            await message.reply_text(
                "🛑 **Please join our channel to get search results.**\n\nClick the button below to join:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 Join Channel", url=invite_link)]]),
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            return

        # ------------------------------
        # PRIMARY SEARCH
        # ------------------------------
        try:
            ranked_results = await search_series(query)
        except Exception as e:
            logger.error(f"Search failed for query `{query}`: {e}")
            ranked_results = []

        # ------------------------------
        # NO RESULT → SHOW TOP SUGGESTIONS
        # ------------------------------
        if not ranked_results:
            # Fuzzy search itself gives suggestions now
            suggestion_list = await search_series(query)

            if suggestion_list:
                # show max 5 suggestions
                suggestion_list = list(dict.fromkeys(suggestion_list))[:5]

                buttons = [
                    [InlineKeyboardButton(text=s, callback_data=f"sugg_select|{message.from_user.id}|{s}")]
                    for s in suggestion_list
                ]
                buttons.append([InlineKeyboardButton("Close", callback_data=f"close_suggestion|{message.from_user.id}")])

                sent = await message.reply(
                    f"❌ No exact match found for `<code>{query}</code>`\n\n🔍 **Did you mean:**",
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

                auto_delete = await get_global_setting("AUTO_DELETE_TIMER", 0)
                if auto_delete and int(auto_delete) > 0:
                    asyncio.create_task(delete_message_later(cyber, sent.chat.id, sent.id, int(auto_delete)))
                return
            else:
                await message.reply_text(
                    f"❌ No results found for `<code>{query}</code>`",
                    parse_mode=enums.ParseMode.HTML,
                )
                return

        # ------------------------------
        # RANKED HIT → SHOW THE BEST RESULT
        # ------------------------------
        best_match = clean_series_name(ranked_results[0])

        # POSTER (cache + OMDB)
        poster = DEFAULT_POSTER
        imdb_enabled = True
        try:
            imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
        except: pass

        try:
            cache = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
            with shelve.open(cache) as db:
                key = f"poster::{best_match.lower()}"
                if key in db:
                    poster = db[key]
        except: pass

        if (not poster or poster == DEFAULT_POSTER) and imdb_enabled:
            try:
                temp = await get_series_poster_async(best_match)
                poster = temp or DEFAULT_POSTER
            except: poster = DEFAULT_POSTER

        # SUMMARY
        summary = await get_series_summary(best_match)

        languages_list = ", ".join(summary.get("languages", []))
        seasons_count = len(summary.get("seasons", []))
        qualities_list = ", ".join(summary.get("qualities", []))

        txt = (
            f"**○ Title:** {best_match}\n"
            f"**○ Language:** {languages_list or 'N/A'}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )

        # Seasons keyboard
        keyboard = []
        best_short = best_match[:MAX_CALLBACK_LENGTH]
        for s in summary.get("seasons", []):
            keyboard.append([InlineKeyboardButton(f"Season {s}", callback_data=f"ss|{best_short}|{s}")])

        kb = InlineKeyboardMarkup(keyboard)

        try:
            sent_msg = await message.reply_photo(
                poster,
                caption=txt,
                reply_markup=kb,
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except:
            sent_msg = await message.reply_text(txt, reply_markup=kb, parse_mode=enums.ParseMode.MARKDOWN)

        auto_delete = await get_global_setting("AUTO_DELETE_TIMER", 0)
        if auto_delete and int(auto_delete) > 0:
            asyncio.create_task(delete_message_later(cyber, sent_msg.chat.id, sent_msg.id, int(auto_delete)))

    ############################################################
    # SUGGESTION SELECT (WHEN USER CLICKS "Did you mean?")
    ############################################################
    @cyber.on_callback_query(filters.regex(r"^sugg_select\|"))
    async def suggestion_pick(cyber, query: CallbackQuery):
        data = query.data.split("|", 2)
        if len(data) < 3:
            await query.answer("Invalid.", show_alert=True)
            return

        series_name = data[2]
        await query.answer(f"Loading {series_name}…", cache_time=0)

        # Poster
        poster = DEFAULT_POSTER
        imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)

        try:
            cache = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
            with shelve.open(cache) as db:
                key = f"poster::{series_name.lower()}"
                if key in db:
                    poster = db[key]
        except: pass

        if (not poster or poster == DEFAULT_POSTER) and imdb_enabled:
            try:
                temp = await get_series_poster_async(series_name)
                poster = temp or DEFAULT_POSTER
            except: poster = DEFAULT_POSTER

        summary = await get_series_summary(series_name)
        langs = ", ".join(summary.get("languages", []))
        seasons = len(summary.get("seasons", []))
        qual = ", ".join(summary.get("qualities", []))

        txt = (
            f"**○ Title:** {series_name}\n"
            f"**○ Language:** {langs or 'N/A'}\n"
            f"**○ Season Available:** {seasons}\n"
            f"**○ Quality Available:** {qual or 'N/A'}\n\n"
            f"Select a season 👇"
        )

        buttons = []
        short = series_name[:MAX_CALLBACK_LENGTH]
        for s in summary.get("seasons", []):
            buttons.append([InlineKeyboardButton(f"Season {s}", callback_data=f"ss|{short}|{s}")])

        kb = InlineKeyboardMarkup(buttons)

        try:
            await query.message.edit_media(
                InputMediaPhoto(poster, caption=txt, parse_mode=enums.ParseMode.MARKDOWN),
                reply_markup=kb,
            )
        except:
            await query.message.edit_text(txt, reply_markup=kb, parse_mode=enums.ParseMode.MARKDOWN)

    ############################################################
    # CLOSE SUGGESTION
    ############################################################
    @cyber.on_callback_query(filters.regex(r"^close_suggestion\|"))
    async def close_suggestion(cyber, query: CallbackQuery):
        try:
            await query.message.delete()
            await query.answer("Closed.")
        except:
            await query.answer("Unable to close.", show_alert=True)

    ############################################################
    # SEASON → LANGUAGE MENU
    ############################################################
    @cyber.on_callback_query(filters.regex(r"^ss\|"))
    async def season_select(cyber, query: CallbackQuery):
        data = query.data.split("|")
        if len(data) < 3:
            await query.answer("Invalid.", show_alert=True)
            return

        series = data[1]
        season = int(data[2])

        await show_language_menu(cyber, query, series, season)
        await query.answer()

    ############################################################
    # LANGUAGE → QUALITY MENU
    ############################################################
    @cyber.on_callback_query(filters.regex(r"^ls\|"))
    async def language_select(cyber, query: CallbackQuery):
        data = query.data.split("|")
        if len(data) < 4:
            await query.answer("Invalid.", show_alert=True)
            return

        series = data[1]
        season = int(data[2])
        lang = data[3]

        await show_quality_menu(cyber, query, series, lang, season)
        await query.answer()

    ############################################################
    # QUALITY → SEND ALL FILES
    ############################################################
    @cyber.on_callback_query(filters.regex(r"^qs\|"))
    async def quality_select(cyber, query: CallbackQuery):
        data = query.data.split("|")
        if len(data) < 5:
            await query.answer("Invalid.", show_alert=True)
            return

        series = data[1]
        season = int(data[2])
        lang = data[3]
        quality = data[4]

        await query.answer(f"Sending S{season} {quality}/{lang} episodes…")

        files = await get_all_files_by_season_and_quality(series, season, quality, lang)
        if not files:
            await query.answer("No files found.", show_alert=True)
            return

        bot_username = (await cyber.get_me()).username
        sent = 0

        for f in files:
            ch = f["channel_id"]
            msg = f["message_id"]
            fname = f.get("file_name", f"S{season}E{f.get('episode', '?')}")

            caption = CUSTOM_CAPTION_TEXT.format(file_name=fname, bot_username=bot_username)

            try:
                fid = f.get("file_id")
                if fid:
                    await cyber.send_document(query.from_user.id, fid, caption=caption)
                else:
                    await cyber.copy_message(query.from_user.id, ch, msg, caption=caption)
                sent += 1
            except Exception as e:
                logger.error(f"Send error {msg}: {e}")
                continue

            if sent < len(files):
                await asyncio.sleep(1.5)

        await cyber.send_message(query.from_user.id,
            f"✔️ Sent {sent} files from **{series}** S{season} ({quality})",
            parse_mode=enums.ParseMode.MARKDOWN)

    ############################################################
    # SINGLE FILE SEND (fs|)
    ############################################################
    @cyber.on_callback_query(filters.regex(r"^fs\|"))
    async def send_single(cyber, query: CallbackQuery):
        data = query.data.split("|")
        if len(data) < 4:
            await query.answer("Invalid.", show_alert=True)
            return

        channel = int(data[2])
        msg_id = int(data[3])

        entry = await get_file_by_message_id_and_channel_id(channel, msg_id)
        if not entry:
            await query.answer("Not found.", show_alert=True)
            return

        fname = entry.get("file_name", "Episode")
        caption = CUSTOM_CAPTION_TEXT.format(file_name=fname, bot_username=(await cyber.get_me()).username)

        try:
            await cyber.copy_message(query.from_user.id, channel, msg_id, caption=caption)
            await query.answer(f"Sent {fname}")
        except Exception as e:
            logger.error(e)
            await query.answer("Failed.", show_alert=True)

    ############################################################
    # MENU PROXIES
    ############################################################
    @cyber.on_callback_query(filters.regex(r"^help\|"))
    async def help_proxy(cyber, query): await help_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^cyber\|"))
    async def cyber_proxy(cyber, query): await cyber_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^admin_cmd\|"))
    async def admin_proxy(cyber, query): await admin_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^about\|"))
    async def about_proxy(cyber, query): await about_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"close"))
    async def close_proxy(cyber, query): await close_menu_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^bl\|"))
    async def back_lang_proxy(cyber, query): await back_to_language_handler(cyber, query)

    @cyber.on_callback_query(filters.regex(r"^back_to_search\|"))
    async def back_search_proxy(cyber, query): await back_to_search_handler(cyber, query)


    return [
        search_handler, suggestion_pick, close_suggestion,
        season_select, language_select, quality_select,
        send_single,
        help_proxy, cyber_proxy, admin_proxy,
        about_proxy, close_proxy,
        back_lang_proxy, back_search_proxy
    ]
