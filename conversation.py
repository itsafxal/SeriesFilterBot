import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from database import search_series, get_unique_seasons_by_name, get_all_files_by_season, logger
from config import logger

def register_conversation_handlers(app: Client):

    @app.on_message(filters.command("start") & filters.private)
    async def start_command(_, message):
        """Handles the /start command."""
        welcome_text = (
            "👋 <b>Welcome! I'm your Series File Filter Bot.</b>\n\n"
            "Just send me the <b>name of a series</b> you are looking for, "
            "and I'll show you the available seasons and episodes from the index.\n\n"
            "<b>Example:</b> <code>Loki</code> or <code>Money Heist</code>"
        )
        await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

    # conversation.py - MODIFIED search_handler

@app.on_message(filters.text & filters.private & ~filters.command("start"))
async def search_handler(_, message):
    """Handles user text search queries and post-filters for unification."""
    query = message.text.strip()
    
    unique_names = await search_series(query) 
    
    if unique_names:
        # --- NEW POST-FILTERING LOGIC ---
        # This uses the common practice of showing the shortest or most popular name
        # when many near-duplicates are returned (e.g., The Witcher vs The Witcher A Sermon)
        
        base_series_names = {}
        for name in unique_names:
            # Strip everything after a common separator (colon, hyphen) or excessive spaces
            base_name = re.sub(r'[:—\-].*$', '', name).strip()
            base_name = re.sub(r'\s{3,}.*$', '', base_name).strip() # Too many spaces means subtitle
            
            # The name 'The Witcher' is likely the base name for any name starting with it.
            if name.lower().startswith('the witcher'):
                base_name = 'The Witcher'
            
            # Use the shortest name as the most unified version
            if base_name not in base_series_names or len(name) < len(base_series_names[base_name]):
                base_series_names[base_name] = name # Store the original name that resulted in this base_name
        
        # Now, use the keys (the unified names) as the final list to display
        # We need to map the unified name back to the original DB name to select the season menu
        # Since the 'normalized_name' in the DB is still the long one, we MUST use the long name
        # for the callback data until re-indexing is complete.
        
        final_display_names = list(base_series_names.keys())
        # --- END POST-FILTERING LOGIC ---

        keyboard = []
        # Use the *unified* name for the button text, but the *original* name for the callback data
        for unified_name in sorted(final_display_names):
            # Find an original DB name that matches this unified name (we will use the shortest one stored in the dict value)
            original_db_name = base_series_names[unified_name]
            
            # NOTE: After re-indexing with the fixed utils.py, the unified_name will be identical
            # to the original_db_name, making this simpler.
            
            # Use the *original DB name* for the callback, as the DB still stores the long name
            callback_data = f'series_select|{original_db_name}' 
            
            # Use the *unified* name for the button text
            button_text = unified_name[:50] + '...' if len(unified_name) > 50 else unified_name 
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
        reply_markup = InlineKeyboardMarkup(keyboard)

        response = f"🔎 Found {len(final_display_names)} matching series. Please select one:"
        await message.reply_text(response, reply_markup=reply_markup)
    else:
        await message.reply_text(f"❌ No series files found matching '<code>{query}</code>'.", parse_mode=enums.ParseMode.HTML)

    @app.on_callback_query(filters.regex(r"series_select|season_send"))
    async def callback_query_handler(_, query):
        """Handles button clicks for series and season selection."""
        data = query.data.split('|')
        action_type = data[0]
        
        try:
            if action_type == 'series_select':
                series_name = data[1]
                await show_season_menu(query, series_name)
                
            elif action_type == 'season_send':
                series_name = data[1]
                season = int(data[2])
                await send_season_files(query, series_name, season)
        except Exception as e:
            logger.error(f"Error in callback: {e}")
            await query.answer("An internal error occurred. Try again.", show_alert=True)
        
        await query.answer()

    async def show_season_menu(query, series_name: str):
        """Displays buttons for all unique seasons of the selected series in SXX format."""
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
        row = []
        
        for season in seasons:
            callback_data = f'season_send|{series_name}|{season}'
            button_text = f"S{season:02d}" # SXX format
            
            row.append(InlineKeyboardButton(button_text, callback_data=callback_data))
            
            if len(row) >= 4: 
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        # Add Back and Close buttons as a final row
        final_row = []
        final_row.append(InlineKeyboardButton("⬅️ BACK", callback_data='go_back_to_search'))
        final_row.append(InlineKeyboardButton("❌ CLOSE", callback_data='close'))
        keyboard.append(final_row)


        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = (
            f"📺 <b>{series_name}</b>\n"
            f"<i>Total Files Indexed: {total_files} in {len(seasons)} seasons.</i>\n\n"
            "Select a Season:"
        )
        
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.HTML)
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


    async def send_season_files(query, series_name: str, season: int):
        """Sends all files belonging to the selected season using copy_message."""
        user_chat_id = query.message.chat.id
        files = await get_all_files_by_season(series_name, season) 
        
        if not files:
            await query.message.reply_text(text=f"❌ No files found for <b>{series_name}</b> Season {season:02d}.", parse_mode=enums.ParseMode.HTML) 
            return

        status_message = await query.message.reply_text(
            f"📦 Sending {len(files)} files for <b>{series_name}</b> Season {season:02d}..."
        )

        success_count = 0
        
        for file_entry in files:
            channel_id = file_entry.get('channel_id')
            message_id = file_entry.get('message_id')
            
            if channel_id and message_id:
                try:
                    await app.copy_message(
                        chat_id=user_chat_id,
                        from_chat_id=channel_id,
                        message_id=message_id
                    )
                    success_count += 1
                    await asyncio.sleep(0.5) 
                except Exception as e:
                    logger.error(f"Failed to copy message {message_id} from {channel_id}: {e}")

        final_message = f"✅ Sent <b>{success_count}</b> files for <b>{series_name}</b> Season {season:02d}."
        
        try:
            await status_message.edit_text(final_message, parse_mode=enums.ParseMode.HTML) 
        except Exception:
            await query.message.reply_text(final_message, parse_mode=enums.ParseMode.HTML) 

    return [start_command, search_handler, callback_query_handler, go_back_to_search_handler, close_menu_handler]
