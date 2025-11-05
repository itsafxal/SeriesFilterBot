# conversation.py (IMDB POSTER REMOVED)

import asyncio
import re
import requests
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto
)
from pyrogram.errors import MessageNotModified
from database import (
    search_series,
    get_all_files_by_season_and_quality,
    get_series_summary,
    get_global_setting,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
    get_file_by_message_id_and_channel_id
)
from config import logger, CUSTOM_CAPTION_TEXT, INDEX_CHANNELS
from utils import clean_series_name, save_series_file_for_bulk_index
from movie_fetcher import get_imdb_data # Kept for get_imdb_data usage (for link and data, not poster)

MAX_CALLBACK_LENGTH = 50


async def delete_message_later(app: Client, chat_id, message_id, delay):
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    try:
        await app.delete_messages(chat_id, message_id)
    except:
        pass


async def show_language_menu(app: Client, query, series_name: str, season: int):
    languages = await get_unique_languages_by_season(series_name, season)
    if not languages:
        await query.answer(f"❌ No languages found for {series_name} S{season}.", show_alert=True)
        return
    keyboard = []
    series_name_short = series_name[:MAX_CALLBACK_LENGTH]
    for lang in languages:
        callback_data = f'ls|{series_name_short}|{season}|{lang}'
        display_text = f"🗣️ {lang}"
        keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f'back_to_search|{series_name_short}')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    caption = (
        f"🎬 **{series_name} - S{season}**\n"
        f"Select the **Language**:"
    )

    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        # Check for message.photo and change to edit_text
        if query.message.photo:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass # Handle case where text is the same
    except Exception:
        pass

    if auto_delete_timer > 0:
        asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer))



async def show_quality_menu(app: Client, query, series_name: str, language: str, season: int):
    qualities = await get_unique_qualities_by_lang_season(series_name, language, season)

    if not qualities:
        await query.answer(f"❌ No quality files found for S{season} ({language}).", show_alert=True)
        return

    keyboard = []
    series_name_short = series_name[:MAX_CALLBACK_LENGTH]
    for quality in qualities:
        callback_data = f'qs|{series_name_short}|{season}|{language}|{quality}'
        keyboard.append([InlineKeyboardButton(f"💾 {quality}", callback_data=callback_data)])

    keyboard.append([InlineKeyboardButton(f"⬅️ Back", callback_data=f'ls|{series_name_short}|{season}|{language}')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    caption = (
        f"🎬 **{series_name} - S{season}** ({language})\n"
        f"Select the desired **video quality**\n\n"
        f"▶️ After click — bot will auto-send all episodes"
    )

    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        # Check for message.photo and change to edit_text
        if query.message.photo:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        else:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass
    except Exception:
        pass

    if auto_delete_timer > 0:
        asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer))



def register_conversation_handlers(app: Client):

    @app.on_message((filters.document | filters.video | filters.audio) & filters.channel)
    async def media_handler(_, message: Message):
        if message.chat.id in INDEX_CHANNELS and message.media:
            saved, count = await save_series_file_for_bulk_index(message)
            if saved:
                logger.info(f"RTI: Indexed {count} new file(s) from channel {message.chat.id}")


    @app.on_message(filters.text & filters.private & ~filters.command(["start", "imdb", "autodel"]))
    async def search_handler(_, message: Message):
        query = message.text.strip()
        if query.startswith('/') and query.lower() not in ["/index"]:
            return

        # imdb_poster_enabled is no longer needed but keeping get_global_setting call for other settings
        # imdb_poster_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)

        initial_message = await message.reply_text(
            f"🔍 **Searching** for `{query}` ...",
            parse_mode=enums.ParseMode.MARKDOWN
        )

        unique_names = await search_series(query)

        if not unique_names:
            await initial_message.edit_text(f"❌ No series found `<code>{query}</code>`", parse_mode=enums.ParseMode.HTML)
            return

        normalized_query = clean_series_name(query).lower()
        series_to_display = None
        for db_name in unique_names:
            cleaned_db_name = clean_series_name(db_name)
            if cleaned_db_name.lower() == normalized_query:
                series_to_display = cleaned_db_name
                break
        if series_to_display is None:
            series_to_display = clean_series_name(min(unique_names, key=len))

        try:
            await initial_message.edit_text(f"✅ Found **{series_to_display}**. Fetching IMDB ...", parse_mode=enums.ParseMode.MARKDOWN)
        except:
            pass

        imdb_data = None
        # Fetching IMDB data for the link, not the poster
        try:
            imdb_data = await asyncio.get_event_loop().run_in_executor(None, get_imdb_data, series_to_display)
        except:
            pass

        # poster_url is removed
        # poster_url = imdb_data['poster'] if imdb_data and imdb_data.get('poster') else None
        imdb_link = imdb_data['link'] if imdb_data and imdb_data.get('link') else None

        summary = await get_series_summary(series_to_display)
        imdb_title = imdb_data.get('title') if imdb_data else series_to_display
        imdb_released = imdb_data.get('year', 'N/A') if imdb_data else 'N/A'
        imdb_genre = imdb_data.get('genres', 'N/A') if imdb_data else 'N/A'
        imdb_rating = imdb_data.get('rating', 'N/A') if imdb_data else 'N/A'
        imdb_link = imdb_data.get('link') if imdb_data else None
        languages_list = ", ".join(summary['languages'])
        seasons_count = len(summary['seasons'])
        qualities_list = ", ".join(summary['qualities'])
        imdb_text = f" | 🔗 [IMDB]({imdb_link})" if imdb_link else ""
        response = (
            f"**○ Title:** {imdb_title}\n"
            f"**○ Released On:** {imdb_released}\n"
            f"**○ Genre:** {imdb_genre}\n"
            f"**○ Rating:** {imdb_rating}\n"
            f"**○ Language:** {lang_info}\n"
            f"**○ Season:** {season_info}\n\n"
            f"**Select the <u>Season</u> to continue:**"
        )
        keyboard = []
        series_name_short = series_to_display[:MAX_CALLBACK_LENGTH]
        for season in summary['seasons']:
            callback_data = f'ss|{series_name_short}|{season}'
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])
        reply_markup = InlineKeyboardMarkup(keyboard)

        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        # Removed all poster downloading and sending logic
        sent_message = await initial_message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)

        if auto_delete_timer > 0:
            asyncio.create_task(delete_message_later(app, sent_message.chat.id, sent_message.id, auto_delete_timer))


    @app.on_callback_query(filters.regex(r"^ss\\|"))
    async def season_select_handler(app, query):
        data = query.data.split("|")
        series_name = data[1]
        season = int(data[2])
        await show_language_menu(app, query, series_name, season)
        await query.answer()


    @app.on_callback_query(filters.regex(r"^ls\\|"))
    async def language_select_handler(app, query):
        data = query.data.split("|")
        series_name = data[1]
        season = int(data[2])
        language = data[3]
        await show_quality_menu(app, query, series_name, language, season)
        await query.answer()


    @app.on_callback_query(filters.regex(r"^qs\\|"))
    async def quality_select_handler(app, query):
        data = query.data.split("|")
        series_name = data[1]
        season = int(data[2])
        language = data[3]
        quality = data[4]

        await query.answer(f"Sending {quality}/{language} episodes for S{season}...", cache_time=0)

        files = await get_all_files_by_season_and_quality(series_name, season, quality, language)

        if not files:
            await query.answer("❌ No files found.", show_alert=True)
            return

        bot_username = (await app.get_me()).username

        for file_entry in files:
            channel_id = file_entry["channel_id"]
            message_id = file_entry["message_id"]
            file_name = file_entry.get("file_name", f"S{file_entry.get('season',0)}E{file_entry.get('episode',99)}")

            custom_caption = CUSTOM_CAPTION_TEXT.format(
                file_name=file_name,
                bot_username=bot_username
            )

            try:
                await app.copy_message(
                    chat_id=query.from_user.id,
                    from_chat_id=channel_id,
                    message_id=message_id,
                    caption=custom_caption,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"copy fail {message_id} : {e}")

        await query.answer("✅ Sent files!", show_alert=True)


    @app.on_callback_query(filters.regex(r"^fs\\|"))
    async def file_select_handler(app, query):
        data = query.data.split('|')
        channel_id = int(data[2])
        message_id = int(data[3])
        await query.answer("Sending episode file...", cache_time=0)

        file_entry = await get_file_by_message_id_and_channel_id(channel_id, message_id)

        if not file_entry:
            await query.answer("❌ File link not found.", show_alert=True)
            return

        file_name = file_entry.get('file_name', f"S{file_entry.get('season',0)}E{file_entry.get('episode',99)}")

        try:
            bot_username = (await app.get_me()).username
            custom_caption = CUSTOM_CAPTION_TEXT.format(file_name=file_name, bot_username=bot_username)

            await app.copy_message(
                chat_id=query.from_user.id,
                from_chat_id=channel_id,
                message_id=message_id,
                caption=custom_caption,
                parse_mode=enums.ParseMode.MARKDOWN
            )

            await query.answer(f"✅ Sent '{file_name}'!", show_alert=True)

        except Exception as e:
            logger.error(f"Failed to copy file {message_id} from {channel_id}: {e}")
            await query.answer("❌ Failed to send file.\nMake sure bot is admin.", show_alert=True)


    @app.on_callback_query(filters.regex(r"back_to_search"))
    async def back_to_search_handler(app, query):
        data = query.data.split('|')
        series_name = data[1]

        # Removed imdb_poster_enabled check
        imdb_data = None

        try:
            imdb_data = await asyncio.get_event_loop().run_in_executor(None, get_imdb_data, series_name)
        except:
            pass

        # poster_url is removed
        # poster_url = imdb_data['poster'] if imdb_data and imdb_data.get('poster') else None
        imdb_link = imdb_data['link'] if imdb_data and imdb_data.get('link') else None

        summary = await get_series_summary(series_name)
        languages_list = ", ".join(summary['languages'])
        seasons_count = len(summary['seasons'])
        qualities_list = ", ".join(summary['qualities'])
        imdb_text = f" | 🔗 [IMDB]({imdb_link})" if imdb_link else ""

        response = (
            f"🔎 **{series_name}**{imdb_text}\n\n"
            f"🎬 Seasons: **{seasons_count}**\n"
            f"🗣️ Languages: **{languages_list}**\n"
            f"💾 Qualities: **{qualities_list}**\n\n"
            f"⬇️ Select Season:"
        )

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary['seasons']:
            callback_data = f'ss|{series_name_short}|{season}'
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)

        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        try:
            # Removed logic for editing media (photo)
            await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception:
            try:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except:
                pass

        if auto_delete_timer > 0:
            asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer))

        await query.answer()


    @app.on_callback_query(filters.regex(r"close"))
    async def close_menu_handler(_, query):
        try:
            await query.message.delete()
            await query.answer("Closed.")
        except:
            await query.answer("Can't close.")

    return [
        media_handler,
        search_handler,
        season_select_handler,
        language_select_handler,
        quality_select_handler,
        file_select_handler,
        back_to_search_handler,
        close_menu_handler
    ]
