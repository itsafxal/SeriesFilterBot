# conversation.py

import asyncio
import re
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from database import search_series, get_unique_seasons_by_name, get_all_files_by_season
from config import logger, CUSTOM_CAPTION_TEXT 
from utils import clean_series_name 

def register_conversation_handlers(app: Client):

    @app.on_message(filters.command("start") & filters.private)
    async def start_command(_, message):
        """Handles the /start command."""
        welcome_text = (
            "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
            "Just send me the <b>name of a series</b> you are looking for, "
            "and I'll show you the available seasons and episodes from the index.\n\n"
            "<b>Example:</b> <code>Loki</code> or <code>Game of Thrones</code>"
        )
        await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

    @app.on_message(filters.text & filters.private & ~filters.command("start"))
    async def search_handler(_, message):
        """
        FIXED: Applies aggressive post-search unification to ensure only the 
        root series name is shown in the buttons, relying on database.py 
        regex for lookup.
        """
        query = message.text.strip()
        
        # unique_names contains clean, unified names from the DB (e.g., 'Heart Beat Fledgling Bonds')
        unique_names = await search_series(query) 
        
        if unique_names:
            # --- AGGRESSIVE POST-SEARCH UNIFICATION LOGIC (FIX) ---
            unified_map = {}
            for db_name in unique_names:
                # 1. Clean the name aggressively using the helper function
                cleaned_name = clean_series_name(db_name) 

                # 2. Get the root name by taking only the first 2-3 words (e.g., "Heart Beat" from "Heart Beat Fledgling Bonds")
                name_parts = cleaned_name.split()
                
                # Heuristic for determining root name length (e.g., 'Game of Thrones' = 3 words, 'Loki' = 1 word)
                root_word_count = 1 
                if len(name_parts) >= 2:
                    root_word_count = 2
                    # If the second word is a conjunction/preposition, take one more word (e.g., 'of', 'and')
                    if len(name_parts) >= 3 and name_parts[1].lower() in ['of', 'and', 'the', 'a']:
                        root_word_count = 3
                
                # The final display name is the root of the title
                final_root_name = " ".join(name_parts[:root_word_count]).strip()

                # Fallback check
                if len(final_root_name) < 2:
                    final_root_name = cleaned_name # Use the fully cleaned name if the root name is too short

                # 3. Store in the map, using the root name for both the key and the callback data
                if final_root_name:
                     unified_map[final_root_name] = final_root_name

            final_display_names = sorted(list(unified_map.keys()))
            # --- END AGGRESSIVE POST-SEARCH UNIFICATION LOGIC ---

            keyboard = []
            for unified_name in final_display_names:
                # Use the short, unified name for the callback
                callback_data = f'series_select|{unified_name}' 
                button_text = unified_name[:50] + '...' if len(unified_name) > 50 else unified_name 
                keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
                
            reply_markup = InlineKeyboardMarkup(keyboard)

            response = f"🔎 Found {len(final_display_names)} matching series. Please select one:"
            await message.reply_text(response, reply_markup=reply_markup)
        else:
            await message.reply_text(f"❌ No series files found matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)

    @app.on_callback_query(filters.regex(r"series_select"))
    async def series_select_handler(_, query):
        data = query.data.split('|')
        series_name = data[1] # This is the short, unified name (e.g., "Heart Beat")
        await show_season_menu(query, series_name)
        await query.answer()

    @app.on_callback_query(filters.regex(r"season_select"))
    async def season_select_handler(_, query):
        data = query.data.split('|')
        series_name = data[1]
        season = int(data[2])
        await show_episode_menu(query, series_name, season)
        await query.answer()

    @app.on_callback_query(filters.regex(r"send_single_episode"))
    async def send_single_episode_handler(app, query):
        """Handles sending a single file after selecting an episode, applying a custom caption."""
        data = query.data.split('|')
        channel_id = int(data[1])
        message_id = int(data[2])
        user_chat_id = query.message.chat.id
        
        await query.answer("📦 Sending file...", cache_time=0)

        try:
            # 1. Get the message object to retrieve the original file name
            message_to_copy = await app.get_messages(
                chat_id=channel_id,
                message_ids=message_id
            )
            
            # 2. Extract file name based on media type
            file_name = ""
            if message_to_copy.document:
                file_name = message_to_copy.document.file_name or ""
            elif message_to_copy.video:
                file_name = message_to_copy.video.file_name or ""
            elif message_to_copy.audio:
                file_name = message_to_copy.audio.file_name or ""
            
            # 3. Format the custom caption
            bot_username = (await app.get_me()).username
            custom_caption = CUSTOM_CAPTION_TEXT.format(
                file_name=file_name,
                bot_username=bot_username
            )
            
            # 4. Copy the message with the new caption
            await app.copy_message(
                chat_id=user_chat_id,
                from_chat_id=channel_id,
                message_id=message_id,
                caption=custom_caption, 
                parse_mode=enums.ParseMode.MARKDOWN 
            )
        except Exception as e:
            logger.error(f"Failed to copy single message {message_id} from {channel_id}: {e}")
            await query.answer("❌ Failed to send file. It might be inaccessible or the caption formatting failed.", show_alert=True)


    async def show_season_menu(query, series_name: str):
        """Displays buttons for all unique seasons of the selected series."""
        # This uses the short series_name (e.g., "Heart Beat") and relies on database.py's regex to find all variants.
        seasons = await get_unique_seasons_by_name(series_name)
        
        if not seasons:
            try:
                await query.message.edit_text(text=f"❌ No seasons found for <b>{series_name}</b>.", parse_mode=enums.ParseMode.HTML)
            except MessageNotModified:
                pass
            return

        tasks = [get_all_files_by_season(series_name, s) for s in seasons]
        all_season_files = await asyncio.gather(*tasks)
        total_files = sum(len(files_list) for files_list in all_season_files)
        
        keyboard = []
        
        for season in seasons:
            callback_data = f'season_select|{series_name}|{season}'
            button_text = f"Season {season}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        keyboard.append([InlineKeyboardButton("🚨 Re Fetch Again 🚨", callback_data='go_back_to_search')])
        
        keyboard.append([
            InlineKeyboardButton("🕹️ Browse Series 🕹️", callback_data='go_back_to_search'),
            InlineKeyboardButton("IMDb Trailer ↗️", url="https://www.imdb.com/find?q=" + series_name.replace(" ", "%20"))
        ])

        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = (
            f"🎬 **{series_name}**\n"
            f"<i>Total Files Indexed: {total_files} in {len(seasons)} seasons.</i>\n\n"
            "Select a Season:"
        )
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass

    async def show_episode_menu(query, series_name: str, season: int):
        """Displays a menu with buttons for all episodes in the selected season."""
        
        # Uses the short series_name (e.g., "Heart Beat") and relies on database.py's regex to find all variants.
        files = await get_all_files_by_season(series_name, season)
        
        if not files:
            await query.message.edit_text(text=f"❌ No episodes found for <b>{series_name}</b> Season {season:02d}.", parse_mode=enums.ParseMode.HTML)
            return

        keyboard = []
        
        for file_entry in files:
            channel_id = file_entry.get('channel_id')
            message_id = file_entry.get('message_id')
            
            full_episode_name = file_entry.get('file_name', f"S{season:02d}E{file_entry.get('episode', 99)}") 
            
            callback_data = f'send_single_episode|{channel_id}|{message_id}'
            button_text = full_episode_name[:60].strip()
            
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        keyboard.append([InlineKeyboardButton(f"⬅️ {series_name}", callback_data=f'series_select|{series_name}')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = f"🎬 **{series_name} - Season {season}**\n\n_Tap an episode to download/send it._"
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass


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


    return [start_command, search_handler, series_select_handler, season_select_handler, send_single_episode_handler, go_back_to_search_handler, close_menu_handler]
