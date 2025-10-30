# conversation.py

import asyncio
import re
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from database import (
    search_series, 
    get_unique_seasons_by_name, 
    get_unique_qualities_by_season, # <-- New Import
    get_all_files_by_season_and_quality # <-- Modified Import
) 
from config import logger, CUSTOM_CAPTION_TEXT 
from utils import clean_series_name 

def register_conversation_handlers(app: Client):

    # ... (start_command remains the same)

    @app.on_message(filters.text & filters.private & ~filters.command("start"))
    async def search_handler(_, message):
        """
        Handles user text search queries, applying aggressive post-search unification 
        to ensure only the root series name is shown.
        """
        query = message.text.strip()
        unique_names = await search_series(query) 
        
        if unique_names:
            # --- AGGRESSIVE POST-SEARCH UNIFICATION LOGIC ---
            unified_map = {}
            for db_name in unique_names:
                cleaned_name = clean_series_name(db_name) 
                
                name_parts = cleaned_name.split()
                root_word_count = 1 
                if len(name_parts) >= 2:
                    root_word_count = 2
                    if len(name_parts) >= 3 and name_parts[1].lower() in ['of', 'and', 'the', 'a']:
                        root_word_count = 3
                
                final_root_name = " ".join(name_parts[:root_word_count]).strip()

                if len(final_root_name) < 2:
                    final_root_name = cleaned_name

                if final_root_name:
                     unified_map[final_root_name] = final_root_name

            final_display_names = sorted(list(unified_map.keys()))
            # --- END AGGRESSIVE POST-SEARCH UNIFICATION LOGIC ---

            keyboard = []
            for unified_name in final_display_names:
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
        series_name = data[1]
        await show_season_menu(query, series_name)
        await query.answer()

    @app.on_callback_query(filters.regex(r"season_select"))
    async def season_select_handler(_, query):
        """NEW FLOW: Leads to the Quality Menu instead of episodes."""
        data = query.data.split('|')
        series_name = data[1]
        season = int(data[2])
        
        await show_quality_menu(query, series_name, season) # <-- NEW STEP
        await query.answer()

    @app.on_callback_query(filters.regex(r"quality_select")) # <-- NEW HANDLER
    async def quality_select_handler(_, query):
        """NEW HANDLER: Leads to the final Episode List."""
        data = query.data.split('|')
        series_name = data[1]
        season = int(data[2])
        quality = data[3]
        
        await show_episode_menu(query, series_name, season, quality)
        await query.answer()
        
    # ... (send_single_episode_handler remains the same)

    async def show_season_menu(query, series_name: str):
        # ... (This function remains the same, showing season buttons)
        seasons = await get_unique_seasons_by_name(series_name)
        
        if not seasons:
            try:
                await query.message.edit_text(text=f"❌ No seasons found for <b>{series_name}</b>.", parse_mode=enums.ParseMode.HTML)
            except MessageNotModified:
                pass
            return

        # Simplified logic for total file count (doesn't need to count quality variants here)
        total_files_estimate = 0
        for s in seasons:
             qualities = await get_unique_qualities_by_season(series_name, s)
             if qualities:
                 # Count the number of files for the highest quality available in that season
                 highest_quality_files = await get_all_files_by_season_and_quality(series_name, s, qualities[0])
                 total_files_estimate += len(highest_quality_files)
        
        keyboard = []
        
        for season in seasons:
            # Callback is now season_select|<series_name>|<season>
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
            f"<i>Total Episodes Indexed: {total_files_estimate} in {len(seasons)} seasons.</i>\n\n"
            "Select a Season:"
        )
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
            

    async def show_quality_menu(query, series_name: str, season: int):
        """NEW FUNCTION: Displays buttons for all unique qualities available for the selected season."""
        
        qualities = await get_unique_qualities_by_season(series_name, season)
        
        if not qualities:
            try:
                await query.message.edit_text(text=f"❌ No files found with a recognized quality for <b>{series_name}</b> Season {season}.", parse_mode=enums.ParseMode.HTML)
            except MessageNotModified:
                pass
            return
            
        keyboard = []
        for quality in qualities:
            # Callback is now quality_select|<series_name>|<season>|<quality>
            callback_data = f'quality_select|{series_name}|{season}|{quality}'
            button_text = f"💾 {quality}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        # Button to go back to the season list
        keyboard.append([InlineKeyboardButton(f"⬅️ Back to Seasons", callback_data=f'series_select|{series_name}')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = (
            f"🎬 **{series_name} - Season {season}**\n"
            f"Select the desired video quality:"
        )
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass


    async def show_episode_menu(query, series_name: str, season: int, quality: str):
        """MODIFIED: Displays a menu with buttons for all episodes in the selected season AND quality."""
        
        files = await get_all_files_by_season_and_quality(series_name, season, quality) # <-- Uses new filter function
        
        if not files:
            await query.message.edit_text(text=f"❌ No episodes found for <b>{series_name}</b> Season {season:02d} in **{quality}**.", parse_mode=enums.ParseMode.HTML)
            return

        keyboard = []
        
        for file_entry in files:
            channel_id = file_entry.get('channel_id')
            message_id = file_entry.get('message_id')
            full_episode_name = file_entry.get('file_name', f"S{season:02d}E{file_entry.get('episode', 99)}") 
            callback_data = f'send_single_episode|{channel_id}|{message_id}'
            button_text = full_episode_name[:60].strip()
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        # Button to go back to the quality list
        keyboard.append([InlineKeyboardButton(f"⬅️ Back to Quality", callback_data=f'season_select|{series_name}|{season}')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = f"🎬 **{series_name} - S{season} ({quality})**\n\n_Tap an episode to download/send it._"
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass


    @app.on_callback_query(filters.regex(r"go_back_to_search"))
    async def go_back_to_search_handler(_, query):
        # ... (This function remains the same)
        try:
            await query.message.edit_text(
                "Send a new series name to search for files."
            )
            await query.answer()
        except Exception:
            await query.answer("Cannot go back to search results.", show_alert=True)

    @app.on_callback_query(filters.regex(r"close"))
    async def close_menu_handler(_, query):
        # ... (This function remains the same)
        try:
            await query.message.delete()
            await query.answer("Menu closed.")
        except Exception:
            await query.answer("Could not close the menu.")


    return [start_command, search_handler, series_select_handler, season_select_handler, quality_select_handler, send_single_episode_handler, go_back_to_search_handler, close_menu_handler]
