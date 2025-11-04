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
    get_unique_languages_by_name, 
    get_unique_seasons_by_name, 
    get_unique_qualities_by_season, 
    get_all_files_by_season_and_quality,
    get_series_summary 
) 
from config import logger, CUSTOM_CAPTION_TEXT 
from utils import clean_series_name 
from movie_fetcher import get_imdb_data # Import from the renamed file

def register_conversation_handlers(app: Client):

    # Utility constant for shorter callback data prefixes
    MAX_CALLBACK_LENGTH = 50

    @app.on_message(filters.command("start") & filters.private)
    async def start_command(_, message):
        """Handles the /start command."""
        welcome_text = (
            "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
            "Just send me the <b>name of a series</b> you are looking for. "
            "I'll show you a summary and ask you to select the language."
        )
        await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

    @app.on_message(filters.text & filters.private & ~filters.command("start"))
    async def search_handler(_, message):
        """
        Handles user text search queries, finds the primary series name, 
        fetches IMDB poster, and shows the summary followed by language buttons.
        """
        query = message.text.strip()
        
        if query.startswith('/') and query.lower() not in ["/index"]: 
             return

        unique_names = await search_series(query) 
        
        if unique_names:
            normalized_query = clean_series_name(query).lower()
            series_to_display = None
            
            # Logic to find the best, most generic series name
            for db_name in unique_names:
                cleaned_db_name = clean_series_name(db_name)
                if cleaned_db_name.lower() == normalized_query:
                    series_to_display = cleaned_db_name
                    break 
                if series_to_display is None and cleaned_db_name.lower().startswith(normalized_query):
                    series_to_display = cleaned_db_name
                    
            if series_to_display is None:
                series_to_display = clean_series_name(min(unique_names, key=len)) if unique_names else None

            if series_to_display:
                
                # --- Get IMDB Data asynchronously ---
                imdb_data = None
                try:
                    imdb_data = await asyncio.get_event_loop().run_in_executor(
                        None, get_imdb_data, series_to_display
                    )
                except Exception as e:
                    logger.error(f"IMDB data fetch failed for {series_to_display}: {e}")
                
                poster_url = imdb_data['poster'] if imdb_data and imdb_data.get('poster') else None
                imdb_link = imdb_data['link'] if imdb_data and imdb_data.get('link') else None
                # --------------------------------------

                # Get summary data (languages, seasons, qualities)
                summary = await get_series_summary(series_to_display)

                if not summary['languages']:
                    await message.reply_text(f"❌ Found series **{series_to_display}**, but no index data is available.", parse_mode=enums.ParseMode.MARKDOWN)
                    return
                    
                # --- CUSTOM DISPLAY FOR UNKNOWN LANGUAGES ---
                display_languages = []
                for lang in summary['languages']:
                    if lang == "Unknown":
                        display_languages.append(f"❓ {series_to_display} Files")
                    else:
                        display_languages.append(f"🗣️ {lang}")
                
                languages_list = ", ".join([lang.replace('🗣️ ', '').replace('❓ ', '') for lang in display_languages])
                seasons_count = len(summary['seasons'])
                qualities_list = ", ".join(summary['qualities'])
                
                # Add IMDB Link to the response text if available
                imdb_text = f" | 🔗 [IMDB]({imdb_link})" if imdb_link else ""

                response = (
                    f"🔎 **{series_to_display}**{imdb_text}\n\n"
                    f"🎬 Available Seasons: **{seasons_count}**\n"
                    f"💾 Available Qualities: **{qualities_list}**\n\n"
                    f"🗣️ Select **Language** (Available: {languages_list}):"
                )
                
                keyboard = []
                series_name_short = series_to_display[:MAX_CALLBACK_LENGTH]
                
                for i, lang in enumerate(summary['languages']):
                    callback_data = f'ls|{series_name_short}|{lang}'
                    button_text = display_languages[i] 
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
                
                reply_markup = InlineKeyboardMarkup(keyboard)

                # --- Send Photo if poster is available, otherwise use reply_text ---
                if poster_url:
                    try:
                        # 1. Send the photo directly from URL. This is most reliable for initial messages.
                        sent_message = await message.reply_photo(
                            photo=poster_url, 
                            caption=response,
                            reply_markup=reply_markup, # Send keyboard here to reduce one API call
                            parse_mode=enums.ParseMode.MARKDOWN
                        )
                    except Exception as e:
                        logger.error(f"FATAL: Photo sending failed for {series_to_display}. URL: {poster_url}. Error: {e}")
                        # 2. Fallback: Send text message if photo fails
                        await message.reply_text(
                            f"⚠️ **Could not load poster image.**\n\n{response}", 
                            reply_markup=reply_markup, 
                            parse_mode=enums.ParseMode.MARKDOWN
                        )
                else:
                    # 3. Send text message if no poster URL was found
                    await message.reply_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            
            else:
                await message.reply_text(f"❌ No series name could be isolated matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)
                
        else:
            await message.reply_text(f"❌ No series files found matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)


    # HANDLER: Language selection (Prefix: 'ls') -> moves to season menu
    @app.on_callback_query(filters.regex(r"ls\|"))
    async def language_select_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        language = data[2]
        await show_season_menu(query, series_name, language)
        await query.answer()

    # HANDLER: Season selection (Prefix: 'ss') -> moves to quality menu
    @app.on_callback_query(filters.regex(r"ss\|"))
    async def season_select_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        language = data[2]
        season = int(data[3])
        await show_quality_menu(query, series_name, language, season) 
        await query.answer()

    # HANDLER: Quality selection (Prefix: 'qs') -> sends files
    @app.on_callback_query(filters.regex(r"qs\|")) 
    async def quality_select_handler(app, query):
        """Retrieves all files for the selected season/quality/language and sends them."""
        data = query.data.split('|')
        series_name = data[1]
        language = data[2] 
        season = int(data[3])
        quality = data[4]
        user_chat_id = query.message.chat.id
        
        await query.answer(f"📦 Sending all {quality}/{language} files for S{season}...", cache_time=0)
        
        files = await get_all_files_by_season_and_quality(series_name, season, quality, language) 
        
        if not files:
            await app.send_message(user_chat_id, f"❌ No episodes found for **{series_name}** S{season} ({quality}, {language}).", parse_mode=enums.ParseMode.MARKDOWN)
            return
            
        # Compile and display list of files to be sent
        file_list_display = ""
        for i, file_entry in enumerate(files, 1):
            file_list_display += f"{i}. **E{file_entry.get('episode', 99):02d}**: `{file_entry.get('file_name', 'Unknown File Name')}`\n"
            if i >= 10 and len(files) > 10: 
                file_list_display += f"...and {len(files) - i} more files.\n"
                break
        
        try:
            # Determine if we should edit the caption (if photo exists) or the text
            edit_function = query.message.edit_caption if query.message.photo else query.message.edit_text
            
            await edit_function(
                f"📤 **Sending {len(files)} files to your PM:**\n\n"
                f"🎬 **{series_name} - S{season} ({quality}, {language})**\n\n"
                f"**Files to be Sent:**\n"
                f"{file_list_display}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"⬅️ Back to Languages", callback_data=f'back_to_search|{series_name}')], 
                    [InlineKeyboardButton("🗑️ Close Menu", callback_data='close')]
                ]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message to show sending status: {e}")
            
        total_sent = 0
        for file_entry in files:
            channel_id = file_entry.get('channel_id')
            message_id = file_entry.get('message_id')
            file_name = file_entry.get('file_name', f"S{season:02d}E{file_entry.get('episode', 99)}")
            
            try:
                bot_username = (await app.get_me()).username
                custom_caption = CUSTOM_CAPTION_TEXT.format(
                    file_name=file_name,
                    bot_username=bot_username
                )
                
                await app.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id,
                    caption=custom_caption, 
                    parse_mode=enums.ParseMode.MARKDOWN 
                )
                total_sent += 1
                await asyncio.sleep(1) 
            except Exception as e:
                logger.error(f"Failed to copy file {message_id} from {channel_id}: {e}")
            
        await app.send_message(
            user_chat_id, 
            f"✅ **Transmission Complete!** Sent {total_sent} files for **{series_name}** S{season} ({quality}, {language}).", 
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # HANDLER: Back button from deep in the menu structure -> goes back to initial summary/language list
    @app.on_callback_query(filters.regex(r"back_to_search"))
    async def back_to_search_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        
        # --- Get IMDB Data asynchronously ---
        imdb_data = None
        try:
            imdb_data = await asyncio.get_event_loop().run_in_executor(
                None, get_imdb_data, series_name
            )
        except Exception as e:
            logger.error(f"IMDB data fetch failed on back_to_search for {series_name}: {e}")
        
        poster_url = imdb_data['poster'] if imdb_data and imdb_data.get('poster') else None
        imdb_link = imdb_data['link'] if imdb_data and imdb_data.get('link') else None
        # ------------------------------------------------------
        
        # Recalculate summary for the response
        summary = await get_series_summary(series_name)

        # --- CUSTOM DISPLAY FOR UNKNOWN LANGUAGES ---
        display_languages = []
        for lang in summary['languages']:
            if lang == "Unknown":
                display_languages.append(f"❓ {series_name} Files")
            else:
                display_languages.append(f"🗣️ {lang}")
        
        languages_list = ", ".join([lang.replace('🗣️ ', '').replace('❓ ', '') for lang in display_languages])
        seasons_count = len(summary['seasons'])
        qualities_list = ", ".join(summary['qualities'])
        
        # Add IMDB Link to the response text if available
        imdb_text = f" | 🔗 [IMDB]({imdb_link})" if imdb_link else ""

        response = (
            f"🔎 **{series_name}**{imdb_text}\n\n"
            f"🎬 Available Seasons: **{seasons_count}**\n"
            f"💾 Available Qualities: **{qualities_list}**\n\n"
            f"🗣️ Select **Language** (Available: {languages_list}):"
        )
        
        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for i, lang in enumerate(summary['languages']):
            callback_data = f'ls|{series_name_short}|{lang}'
            button_text = display_languages[i]
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            # CRITICAL: Use edit_media to ensure the photo is updated/retained
            if poster_url:
                await query.message.edit_media(
                    # InputMediaPhoto is needed to pass both the photo (URL) and the caption
                    media=InputMediaPhoto(poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                    reply_markup=reply_markup
                )
            # Fallback to simple text message edit if no poster or original message was text
            else:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            # Fallback for errors like trying to edit a photo that was previously edited to text
            logger.warning(f"Failed to edit media on back_to_search: {e}. Attempting edit_text fallback.")
            try:
                 await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except MessageNotModified:
                pass
        await query.answer()


    # MENU FUNCTION: Season Selection
    async def show_season_menu(query, series_name: str, language: str):
        seasons = await get_unique_seasons_by_name(series_name, language)
        
        if not seasons:
            await query.answer(f"❌ No seasons found for {series_name} ({language}).", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in seasons:
            callback_data = f'ss|{series_name_short}|{language}|{season}' 
            button_text = f"Season {season}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
        # Back button leads to the initial summary/language selection
        keyboard.append([InlineKeyboardButton(f"⬅️ Back to Languages", callback_data=f'back_to_search|{series_name_short}')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        display_language = f"({language})"
        if language == "Unknown":
            display_language = f"(❓ {series_name} Files)"
            
        caption = (
            f"🎬 **{series_name}** {display_language}\n"
            "Select a **Season**:"
        )
        
        try:
            # Use edit_caption if the original message was a photo, edit_text otherwise
            if query.message.photo:
                await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            else:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.warning(f"Error editing menu (show_season_menu): {e}. Falling back to edit_text.")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except MessageNotModified:
                pass
            
    # MENU FUNCTION: Quality Selection
    async def show_quality_menu(query, series_name: str, language: str, season: int):
        qualities = await get_unique_qualities_by_season(series_name, language, season)
        
        if not qualities:
            await query.answer(f"❌ No quality files found for S{season} ({language}).", show_alert=True)
            return
            
        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for quality in qualities:
            callback_data = f'qs|{series_name_short}|{language}|{season}|{quality}'
            button_text = f"💾 {quality}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
        # Back button leads to the Season menu, preserving language context (using 'ls' prefix)
        callback_data_back = f'ls|{series_name_short}|{language}'
        keyboard.append([InlineKeyboardButton(f"⬅️ Back to Seasons", callback_data=callback_data_back)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        display_language = f"({language})"
        if language == "Unknown":
            display_language = f"(❓ {series_name} Files)"
            
        caption = (
            f"🎬 **{series_name} - S{season}** {display_language}\n"
            f"Select the desired **video quality** to **start downloading all episodes**:"
        )
        
        try:
            # Use edit_caption if the original message was a photo, edit_text otherwise
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


    @app.on_callback_query(filters.regex(r"close"))
    async def close_menu_handler(_, query):
        try:
            await query.message.delete()
            await query.answer("Menu closed.")
        except Exception:
            await query.answer("Could not close the menu.")


    # Return all handler functions
    return [
        start_command, search_handler,
        language_select_handler, season_select_handler, 
        quality_select_handler, back_to_search_handler, 
        close_menu_handler
    ]
