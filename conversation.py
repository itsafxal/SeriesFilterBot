# conversation.py

import asyncio
import re
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    InputMediaPhoto
)
from pyrogram.errors import MessageNotModified
from database import (
    search_series, 
    # Kept old imports for summary functionality
    get_unique_languages_by_name, 
    get_unique_seasons_by_name, 
    get_all_files_by_season_and_quality, 
    get_series_summary,
    get_global_setting, 
    set_global_setting,
    # New imports for the Season -> Language -> Quality flow
    get_unique_languages_by_season, 
    get_unique_qualities_by_lang_season,
    # New import for Click-to-Download functionality
    get_file_by_message_id_and_channel_id 
) 
# --- IMPORTANT: ADMIN_USERS must be defined in config.py as a list/tuple of user IDs ---
from config import logger, CUSTOM_CAPTION_TEXT, ADMIN_USERS 
from utils import clean_series_name 
from movie_fetcher import get_imdb_data 

def register_conversation_handlers(app: Client):

    # Utility constant for shorter callback data prefixes
    MAX_CALLBACK_LENGTH = 50

    # --- NEW HELPER FUNCTION FOR AUTO-DELETION ---
    async def delete_message_later(app: Client, chat_id, message_id, delay):
        """Schedules the deletion of a message after a given delay."""
        # Ensure delay is positive before sleeping
        if delay <= 0:
            return
            
        await asyncio.sleep(delay)
        try:
            # We target the specific message ID that was passed, which should be the last edited menu message.
            await app.delete_messages(chat_id, message_id)
            logger.info(f"Auto-deleted menu message {message_id} in chat {chat_id} after {delay}s.")
        except Exception as e:
            # MessageNotModified is fine, but deletion errors should be logged
            logger.warning(f"Failed to auto-delete message {message_id} in chat {chat_id}: {e}")
            
    @app.on_message(filters.command("start") & filters.private)
    async def start_command(_, message):
        """Handles the /start command."""
        welcome_text = (
            "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
            "Just send me the <b>name of a series</b> you are looking for. "
            "I'll show you a summary and ask you to select the **Season**."
        )
        await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

    @app.on_message(filters.command("imdb") & filters.user(ADMIN_USERS) & filters.private)
    async def poster_toggle_command(_, message):
        """Allows admin to turn the IMDB poster feature on or off."""
        current_state = await get_global_setting("IMDB_POSTER_ENABLED", True) 
        new_state = not current_state
        await set_global_setting("IMDB_POSTER_ENABLED", new_state)

        status = "✅ **ON**" if new_state else "❌ **OFF**"
        await message.reply_text(
            f"🖼️ IMDB Poster feature is now set to: {status}",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    @app.on_message(filters.command("autodel") & filters.user(ADMIN_USERS) & filters.private)
    async def autodel_command(_, message):
        """Allows admin to set the auto-delete timer for sent files."""
        try:
            if len(message.command) > 1:
                timer = int(message.command[1])
                if timer < 0:
                    timer = 0
                
                await set_global_setting("AUTO_DELETE_TIMER", timer)
                
                if timer > 0:
                    status = f"✅ **ON**: Menu auto-delete timer set to **{timer} seconds**."
                else:
                    status = "❌ **OFF**: Menu auto-delete is disabled."
                    
                await message.reply_text(
                    f"🗑️ Menu Auto-Deletion feature is now set to: {status}\n\n"
                    f"Set to 0 to disable.",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            else:
                current_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
                status = f"Current menu auto-delete timer is: **{current_timer} seconds**."
                await message.reply_text(
                    f"Usage: `/autodel <seconds>` (e.g., `/autodel 300`)\n"
                    f"{status}",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                
        except ValueError:
            await message.reply_text("❌ Invalid number provided. Please use an integer for seconds.", parse_mode=enums.ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Error in autodel_command: {e}")

    @app.on_message(filters.text & filters.private & ~filters.command(["start", "imdb", "autodel"]))
    async def search_handler(_, message):
        """Handles user text search queries and starts the Season menu."""
        query = message.text.strip()
         
        if query.startswith('/') and query.lower() not in ["/index"]: 
            return

        imdb_poster_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
         
        initial_message = await message.reply_text(
            f"🔍 **Searching** for series matching '`{query}`' in the database. Please wait...",
            parse_mode=enums.ParseMode.MARKDOWN
        )

        unique_names = await search_series(query) 
         
        if unique_names:
            normalized_query = clean_series_name(query).lower()
            series_to_display = None
             
            for db_name in unique_names:
                cleaned_db_name = clean_series_name(db_name)
                if cleaned_db_name.lower() == normalized_query:
                    series_to_display = cleaned_db_name
                    break 
             
            if series_to_display is None:
                series_to_display = clean_series_name(min(unique_names, key=len)) if unique_names else None

            if series_to_display:
                 
                if imdb_poster_enabled:
                    try:
                        await initial_message.edit_text(
                            f"✅ Found **{series_to_display}** in the database. Now fetching IMDB data and poster... 🖼️",
                            parse_mode=enums.ParseMode.MARKDOWN
                        )
                    except MessageNotModified:
                        pass 

                imdb_data = None
                if imdb_poster_enabled:
                    try:
                        imdb_data = await asyncio.get_event_loop().run_in_executor(
                            None, get_imdb_data, series_to_display
                        )
                    except Exception as e:
                        logger.error(f"IMDB data fetch failed for {series_to_display}: {e}")
                     
                poster_url = imdb_data['poster'] if imdb_data and imdb_data.get('poster') else None
                imdb_link = imdb_data['link'] if imdb_data and imdb_data.get('link') else None

                summary = await get_series_summary(series_to_display)

                if not summary['seasons']:
                    await initial_message.edit_text(f"❌ Found series **{series_to_display}**, but no indexed season data is available.", parse_mode=enums.ParseMode.MARKDOWN)
                    return
                     
                languages_list = ", ".join(summary['languages'])
                seasons_count = len(summary['seasons'])
                qualities_list = ", ".join(summary['qualities'])
                 
                imdb_text = f" | 🔗 [IMDB]({imdb_link})" if imdb_link else ""

                response = (
                    f"🔎 **{series_to_display}**{imdb_text}\n\n"
                    f"🎬 Available Seasons: **{seasons_count}**\n"
                    f"🗣️ Available Languages: **{languages_list}**\n"
                    f"💾 Available Qualities: **{qualities_list}**\n\n"
                    f"⬇️ Select **Season** to continue:"
                )
                 
                keyboard = []
                series_name_short = series_to_display[:MAX_CALLBACK_LENGTH]
                 
                for season in summary['seasons']:
                    callback_data = f'ss|{series_name_short}|{season}' 
                    button_text = f"Season {season}"
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
                 
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # --- Get the auto-delete timer for Menus ---
                auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

                if imdb_poster_enabled and poster_url:
                    try:
                        await initial_message.delete() 
                        sent_message = await message.reply_photo( # ⬅ Capture the message
                            photo=poster_url, 
                            caption=response,
                            reply_markup=reply_markup, 
                            parse_mode=enums.ParseMode.MARKDOWN
                        )
                    except Exception as e:
                        logger.error(f"FATAL: Photo sending failed for {series_to_display}. Error: {type(e).__name__}: {e}") 
                        sent_message = await initial_message.edit_text( # ⬅ Capture the message
                            f"⚠️ **Could not load poster image.**\n\n{response}", 
                            reply_markup=reply_markup, 
                            parse_mode=enums.ParseMode.MARKDOWN
                        )
                else:
                    sent_message = await initial_message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN) # ⬅ Capture the message

                # --- Schedule deletion of the initial menu ---
                if auto_delete_timer > 0:
                    asyncio.create_task(
                        delete_message_later(app, sent_message.chat.id, sent_message.id, auto_delete_timer)
                    )
             
            else:
                await initial_message.edit_text(f"❌ No series name could be isolated matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)
             
        else:
            await initial_message.edit_text(f"❌ No series files found matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)


    @app.on_callback_query(filters.regex(r"^ss\|"))
    async def season_select_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        season = int(data[2])
        await show_language_menu(query, series_name, season) 
        await query.answer()

    @app.on_callback_query(filters.regex(r"^ls\|"))
    async def language_select_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        season = int(data[2]) 
        language = data[3] 
        await show_quality_menu(query, series_name, language, season)
        await query.answer()

    # ⬇⬇⬇ MODIFIED: quality_select_handler (REMOVED auto-delete for files)
    @app.on_callback_query(filters.regex(r"^qs\|")) 
    async def quality_select_handler(app, query):
        """QUALITY CLICK = SEND ALL FILES DIRECTLY"""
        data = query.data.split('|')
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

        sent_count = 0
        for file_entry in files:
            channel_id = file_entry["channel_id"]
            message_id = file_entry["message_id"]
            file_name = file_entry.get("file_name", f"S{file_entry.get('season',0)}E{file_entry.get('episode',99)}")

            custom_caption = CUSTOM_CAPTION_TEXT.format(
                file_name=file_name,
                bot_username=bot_username
            )

            try:
                # We no longer capture sent_message or schedule deletion here.
                await app.copy_message( 
                    chat_id=query.from_user.id,
                    from_chat_id=channel_id,
                    message_id=message_id,
                    caption=custom_caption,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                sent_count += 1
            except Exception as e:
                logger.error(f"copy fail {message_id} : {e}")

        await query.answer(f"✅ Sent {sent_count} files to DM!", show_alert=True)

    @app.on_callback_query(filters.regex(r"^fs\|")) 
    async def file_select_handler(app, query):
        """(NOT used now but kept for future)"""
        data = query.data.split('|')
        series_name = data[1]
        channel_id = int(data[2])
        message_id = int(data[3])
        
        await query.answer(f"Sending episode file...", cache_time=0)

        file_entry = await get_file_by_message_id_and_channel_id(channel_id, message_id)
        
        if not file_entry:
            await query.answer("❌ File link not found in database.", show_alert=True)
            return

        file_name = file_entry.get('file_name', f"S{file_entry.get('season', 0)}E{file_entry.get('episode', 99)}")
        
        try:
            bot_username = (await app.get_me()).username
            custom_caption = CUSTOM_CAPTION_TEXT.format(
                file_name=file_name,
                bot_username=bot_username
            )
            
            await app.copy_message(
                chat_id=query.from_user.id,
                from_chat_id=channel_id,
                message_id=message_id,
                caption=custom_caption, 
                parse_mode=enums.ParseMode.MARKDOWN 
            )
        
            await query.answer(f"✅ Sent '{file_name}' to your PM!", show_alert=True)
            
        except Exception as e:
            logger.error(f"Failed to copy file {message_id} from {channel_id} in file_select_handler: {e}")
            await query.answer("❌ Failed to send file. Please ensure the bot is an admin in the source channel.", show_alert=True)
            

    # ⬇⬇⬇ MODIFIED: back_to_search_handler (FIX for IMDB poster not showing)
    @app.on_callback_query(filters.regex(r"back_to_search"))
    async def back_to_search_handler(app, query):
        """Returns to the initial summary/Season selection menu."""
        data = query.data.split('|')
        series_name = data[1]
         
        imdb_poster_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
        imdb_data = None
        if imdb_poster_enabled:
            try:
                imdb_data = await asyncio.get_event_loop().run_in_executor(None, get_imdb_data, series_name)
            except Exception:
                pass
         
        poster_url = imdb_data['poster'] if imdb_data and imdb_data.get('poster') else None
        imdb_link = imdb_data['link'] if imdb_data and imdb_data.get('link') else None
         
        summary = await get_series_summary(series_name)
        languages_list = ", ".join(summary['languages'])
        seasons_count = len(summary['seasons'])
        qualities_list = ", ".join(summary['qualities'])
        imdb_text = f" | 🔗 [IMDB]({imdb_link})" if imdb_link else ""

        response = (
            f"🔎 **{series_name}**{imdb_text}\n\n"
            f"🎬 Available Seasons: **{seasons_count}**\n"
            f"🗣️ Available Languages: **{languages_list}**\n"
            f"💾 Available Qualities: **{qualities_list}**\n\n"
            f"⬇️ Select **Season** to continue:"
        )
         
        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary['seasons']:
            callback_data = f'ss|{series_name_short}|{season}'
            button_text = f"Season {season}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
         
        reply_markup = InlineKeyboardMarkup(keyboard)

        # --- Get the auto-delete timer for Menus ---
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        try:
            # Case 1: Photo exists AND we have a new poster URL, use edit_media to update the photo/caption
            if imdb_poster_enabled and poster_url and query.message.photo:
                await query.message.edit_media(
                    media=InputMediaPhoto(poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                    reply_markup=reply_markup
                )
            # Case 2: Photo exists but we are only editing the CAPTION (FIXED: Uses edit_caption here to keep the photo)
            elif query.message.photo:
                await query.message.edit_caption(
                    caption=response,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            # Case 3: Original message was text-only or fallback required, use edit_text
            else:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.warning(f"Failed to edit media/caption on back_to_search: {e}. Attempting edit_text fallback.")
            try:
                # Final fallback to edit_text, which removes the media if it was a photo
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except MessageNotModified:
                pass
        
        # --- Schedule deletion of the current menu state ---
        if auto_delete_timer > 0:
            asyncio.create_task(
                delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer)
            )
            
        await query.answer()


    # ⬇⬇⬇ show_language_menu (updated for auto-delete)
    async def show_language_menu(query, series_name: str, season: int):
        """Presents languages available for the specified season."""
        languages = await get_unique_languages_by_season(series_name, season)
         
        if not languages:
            await query.answer(f"❌ No languages found for {series_name} S{season}.", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for lang in languages:
            callback_data = f'ls|{series_name_short}|{season}|{lang}' 
            button_text = f"🗣️ {lang}"
            if lang == "Unknown":
                button_text = f"❓ Unknown/Unlabeled Files"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
             
        keyboard.append([InlineKeyboardButton(f"⬅️ Back to All Seasons", callback_data=f'back_to_search|{series_name_short}')])
         
        reply_markup = InlineKeyboardMarkup(keyboard)
         
        caption = (
            f"🎬 **{series_name} - S{season}**\n"
            f"Select the **Language**:"
        )
         
        # --- Get the auto-delete timer for Menus ---
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
        
        try:
            if query.message.photo:
                await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            else:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.warning(f"Error editing menu (show_language_menu): {e}. Falling back to edit_text.")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except MessageNotModified:
                pass
        
        # --- Schedule deletion of the current menu state ---
        if auto_delete_timer > 0:
            asyncio.create_task(
                delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer)
            )
             
    # ⬇⬇⬇ show_quality_menu (updated for auto-delete)
    async def show_quality_menu(query, series_name: str, language: str, season: int):
        """Presents qualities available for the specified season and language."""
        qualities = await get_unique_qualities_by_lang_season(series_name, language, season)
         
        if not qualities:
            await query.answer(f"❌ No quality files found for S{season} ({language}).", show_alert=True)
            return
             
        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for quality in qualities:
            callback_data = f'qs|{series_name_short}|{season}|{language}|{quality}'
            button_text = f"💾 {quality}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
             
        callback_data_back = f'ss|{series_name_short}|{season}' 
        keyboard.append([InlineKeyboardButton(f"⬅️ Back to Languages for S{season}", callback_data=callback_data_back)])
         
        reply_markup = InlineKeyboardMarkup(keyboard)
         
        display_language = f"({language})"
        if language == "Unknown":
            display_language = f"(❓ Files)"
             
        caption = (
            f"🎬 **{series_name} - S{season}** {display_language}\n"
            f"Select the desired **video quality**\n\n"
            f"▶️ After click — bot will auto-send all episodes"
        )
         
        # --- Get the auto-delete timer for Menus ---
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
        
        try:
            if query.message.photo:
                await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            else:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.warning(f"Error editing menu (show_quality_menu): {e}. Falling back to edit_text.")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except MessageNotModified:
                pass
        
        # --- Schedule deletion of the current menu state ---
        if auto_delete_timer > 0:
            asyncio.create_task(
                delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer)
            )


    @app.on_callback_query(filters.regex(r"close"))
    async def close_menu_handler(_, query):
        try:
            await query.message.delete()
            await query.answer("Menu closed.")
        except Exception:
            await query.answer("Could not close the menu.")


    return [
        start_command, poster_toggle_command, autodel_command, search_handler,
        language_select_handler, season_select_handler, 
        quality_select_handler, file_select_handler, 
        back_to_search_handler, 
        close_menu_handler
    ]
