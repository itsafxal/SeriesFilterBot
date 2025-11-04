import asyncio
import re
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from database import (
    search_series, 
    get_unique_languages_by_name, # **ASSUMED NEW FUNCTION**
    get_unique_seasons_by_name, 
    get_unique_qualities_by_season, 
    get_all_files_by_season_and_quality 
) 
from config import logger, CUSTOM_CAPTION_TEXT 
from utils import clean_series_name 

def register_conversation_handlers(app: Client):

    @app.on_message(filters.command("start") & filters.private)
    async def start_command(_, message):
        """Handles the /start command."""
        welcome_text = (
            "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
            "Just send me the <b>name of a series</b> you are looking for, "
            "and I'll show you the available options.\n\n"
            "<b>Example:</b> <code>Loki</code> or <code>Game of Thrones</code>"
        )
        await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

    @app.on_message(filters.text & filters.private & ~filters.command("start"))
    async def search_handler(_, message):
        """
        Handles user text search queries and displays a single button 
        for the core series name.
        """
        query = message.text.strip()
        unique_names = await search_series(query) 
        
        # --- (Existing search and name unification logic remains here) ---
        if unique_names:
            normalized_query = clean_series_name(query).lower()
            series_to_display = None
            
            # 1. Look for the best, most generic name that matches the query
            for db_name in unique_names:
                cleaned_db_name = clean_series_name(db_name)
                if cleaned_db_name.lower() == normalized_query:
                    series_to_display = cleaned_db_name
                    break 
                if series_to_display is None and cleaned_db_name.lower().startswith(normalized_query):
                    series_to_display = cleaned_db_name
                    
            # 2. Fallback: if no clear candidate found, use the shortest cleaned name
            if series_to_display is None:
                series_to_display = clean_series_name(min(unique_names, key=len)) if unique_names else None

            # 3. Display the single button
            if series_to_display:
                keyboard = [
                    [InlineKeyboardButton(series_to_display, callback_data=f'language_menu|{series_to_display}')] # **MODIFIED: changed to language_menu**
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                response = f"🔎 Found **{series_to_display}**. Select the series to view available languages:"
                await message.reply_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            else:
                await message.reply_text(f"❌ No single series name could be isolated from files matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)
                
        else:
            await message.reply_text(f"❌ No series files found matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)

    # **NEW HANDLER: Handles initial series selection and moves to language menu**
    @app.on_callback_query(filters.regex(r"language_menu"))
    async def language_menu_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        await show_language_menu(query, series_name)
        await query.answer()

    # **NEW HANDLER: Handles language selection and moves to season menu**
    @app.on_callback_query(filters.regex(r"language_select"))
    async def language_select_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        language = data[2]
        # Language is now passed to the season menu
        await show_season_menu(query, series_name, language)
        await query.answer()

    # **MODIFIED HANDLER: Now expects language in callback data**
    @app.on_callback_query(filters.regex(r"season_select"))
    async def season_select_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        language = data[2] # **NEW: retrieve language**
        season = int(data[3]) # **Modified index**
        # Language is passed to the quality menu
        await show_quality_menu(query, series_name, language, season) 
        await query.answer()

    # **MODIFIED HANDLER: Now expects language in callback data**
    @app.on_callback_query(filters.regex(r"quality_select")) 
    async def quality_select_handler(app, query):
        """
        Retrieves all files for the selected season/quality/language and sends them 
        directly to the user's private messages.
        """
        data = query.data.split('|')
        series_name = data[1]
        language = data[2] # **NEW: retrieve language**
        season = int(data[3]) # **Modified index**
        quality = data[4] # **Modified index**
        user_chat_id = query.message.chat.id
        
        await query.answer(f"📦 Sending all {quality}/{language} files for Season {season}...", cache_time=0)
        
        # **NOTE: get_all_files_by_season_and_quality must be modified to filter by language as well.**
        # Current call: get_all_files_by_season_and_quality(series_name, season, quality)
        # New call:
        files = await get_all_files_by_season_and_quality(series_name, season, quality, language) 
        
        if not files:
            await app.send_message(user_chat_id, f"❌ No episodes found for **{series_name}** S{season} ({quality}, {language}).", parse_mode=enums.ParseMode.MARKDOWN)
            return
            
        try:
            await query.message.edit_text(
                f"📤 **Sending {len(files)} files to your PM:**\n\n"
                f"🎬 **{series_name} - S{season} ({quality}, {language})**\n\n"
                "_Please wait for all files to arrive. This may take a few minutes._",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"⬅️ Back to Languages", callback_data=f'language_menu|{series_name}')],
                    [InlineKeyboardButton("🗑️ Close Menu", callback_data='close')]
                ]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except MessageNotModified:
            pass
            
        total_sent = 0
        for file_entry in files:
            channel_id = file_entry.get('channel_id')
            message_id = file_entry.get('message_id')
            file_name = file_entry.get('file_name', f"S{season:02d}E{file_entry.get('episode', 99)}")
            
            try:
                # Omitted: app.get_messages for brevity, assume copy_message works
                
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
                await asyncio.sleep(1) # Delay for rate limits
                
            except Exception as e:
                logger.error(f"Failed to copy file {message_id} from {channel_id}: {e}")
                
        await app.send_message(
            user_chat_id, 
            f"✅ **Transmission Complete!** Sent {total_sent} files for **{series_name}** Season {season} ({quality}, {language}).", 
            parse_mode=enums.ParseMode.MARKDOWN
        )

    # --- NEW MENU FUNCTION: Language Selection ---
    async def show_language_menu(query, series_name: str):
        # **ASSUMED NEW FUNCTION USAGE**
        languages = await get_unique_languages_by_name(series_name) 
        
        if not languages:
            try:
                await query.message.edit_text(text=f"❌ No languages found for <b>{series_name}</b>. This series might not be correctly indexed with language tags.", parse_mode=enums.ParseMode.HTML)
            except MessageNotModified:
                pass
            return
            
        keyboard = []
        for lang in languages:
            callback_data = f'language_select|{series_name}|{lang}'
            button_text = f"🗣️ {lang}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = (
            f"🎬 **{series_name}**\n\n"
            "Select the desired **Language**:"
        )
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass

    # --- MODIFIED MENU FUNCTION: Season Selection ---
    async def show_season_menu(query, series_name: str, language: str):
        # **NOTE: get_unique_seasons_by_name must be modified to filter by language**
        seasons = await get_unique_seasons_by_name(series_name, language)
        
        if not seasons:
            try:
                await query.message.edit_text(text=f"❌ No seasons found for <b>{series_name} ({language})</b>.", parse_mode=enums.ParseMode.HTML)
            except MessageNotModified:
                pass
            return

        total_files_estimate = 0 # This calculation will also need to be language-aware

        keyboard = []
        for season in seasons:
            # Callback data must now include the selected language
            callback_data = f'season_select|{series_name}|{language}|{season}' 
            button_text = f"Season {season}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
        keyboard.append([InlineKeyboardButton(f"⬅️ Back to Languages", callback_data=f'language_menu|{series_name}')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = (
            f"🎬 **{series_name} ({language})**\n"
            f"<i>Total Episodes Indexed (estimate): {total_files_estimate} in {len(seasons)} seasons.</i>\n\n"
            "Select a **Season**:"
        )
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
            
    # --- MODIFIED MENU FUNCTION: Quality Selection ---
    async def show_quality_menu(query, series_name: str, language: str, season: int):
        # **NOTE: get_unique_qualities_by_season must be modified to filter by language**
        qualities = await get_unique_qualities_by_season(series_name, language, season)
        
        if not qualities:
            try:
                await query.message.edit_text(text=f"❌ No quality files found for <b>{series_name}</b> Season {season} ({language}).", parse_mode=enums.ParseMode.HTML)
            except MessageNotModified:
                pass
            return
            
        keyboard = []
        for quality in qualities:
            # Callback data must now include the selected language
            callback_data = f'quality_select|{series_name}|{language}|{season}|{quality}'
            button_text = f"💾 {quality}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
        # Back button leads to the Season menu, passing the series name and language
        keyboard.append([InlineKeyboardButton(f"⬅️ Back to Seasons", callback_data=f'language_select|{series_name}|{language}')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = (
            f"🎬 **{series_name} - Season {season} ({language})**\n"
            f"Select the desired **video quality** to **start downloading all episodes**:"
        )
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass


    # --- (Other handlers like send_single_episode_handler and close_menu_handler remain the same) ---
    @app.on_callback_query(filters.regex(r"send_single_episode"))
    async def send_single_episode_handler(app, query):
        data = query.data.split('|')
        channel_id = int(data[1])
        message_id = int(data[2])
        user_chat_id = query.message.chat.id
        
        await query.answer("📦 Sending file...", cache_time=0)

        try:
            message_to_copy = await app.get_messages(
                chat_id=channel_id,
                message_ids=message_id
            )
            
            file_name = message_to_copy.document.file_name or message_to_copy.video.file_name or message_to_copy.audio.file_name or ""
            
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
        except Exception as e:
            logger.error(f"Failed to copy single message {message_id} from {channel_id}: {e}")
            await query.answer("❌ Failed to send file. It might be inaccessible.", show_alert=True)

    @app.on_callback_query(filters.regex(r"go_back_to_search"))
    async def go_back_to_search_handler(_, query):
        try:
            await query.message.edit_text(
                "Send a new series name to search for files."
            )
            await query.answer()
        except Exception:
            await query.answer("Cannot go back to search results.", show_alert=True)

    @app.on_callback_query(filters.regex(r"close"))
    async def close_menu_handler(_, query):
        try:
            await query.message.delete()
            await query.answer("Menu closed.")
        except Exception:
            await query.answer("Could not close the menu.")


    # Return the handler functions (Updated list)
    return [
        start_command, search_handler, 
        language_menu_handler, language_select_handler, # NEW
        season_select_handler, quality_select_handler, 
        send_single_episode_handler, go_back_to_search_handler, close_menu_handler
    ]
