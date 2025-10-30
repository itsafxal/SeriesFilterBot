import asyncio
import re
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
            "<b>Example:</b> <code>Loki</code> or <code>Game of Thrones</code>"
        )
        await message.reply_text(welcome_text, parse_mode=enums.ParseMode.HTML)

    @app.on_message(filters.text & filters.private & ~filters.command("start"))
    async def search_handler(_, message):
        """
        Handles user text search queries and post-filters for unification.
        """
        query = message.text.strip()
        
        unique_names = await search_series(query) 
        
        if unique_names:
            # --- POST-FILTERING LOGIC (Keep as is) ---
            base_series_names = {}
            for name in unique_names:
                base_name = re.sub(r'[:—\-].*$', '', name).strip()
                base_name = re.sub(r'\s{3,}.*$', '', base_name).strip()

                if name.lower().startswith('the witcher'):
                    base_name = 'The Witcher'
                elif name.lower().startswith('loki'):
                    base_name = 'Loki'
                
                if base_name not in base_series_names or len(name) < len(base_series_names[base_name]):
                    base_series_names[base_name] = name 
            
            final_display_names = list(base_series_names.keys())
            # --- END POST-FILTERING LOGIC ---

            keyboard = []
            for unified_name in sorted(final_display_names):
                original_db_name = base_series_names[unified_name]
                callback_data = f'series_select|{original_db_name}' 
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
        data = query.data.split('|')
        series_name = data[1]
        season = int(data[2])
        await show_episode_menu(query, series_name, season)
        await query.answer()

    @app.on_callback_query(filters.regex(r"send_single_episode"))
    async def send_single_episode_handler(app, query):
        """Handles sending a single file after selecting an episode."""
        data = query.data.split('|')
        channel_id = int(data[1])
        message_id = int(data[2])
        user_chat_id = query.message.chat.id
        
        await query.answer("📦 Sending file...", cache_time=0)

        try:
            await app.copy_message(
                chat_id=user_chat_id,
                from_chat_id=channel_id,
                message_id=message_id
            )
            # await query.answer("✅ File sent!", show_alert=False) # Answered above to avoid delay
        except Exception as e:
            logger.error(f"Failed to copy single message {message_id} from {channel_id}: {e}")
            await query.answer("❌ Failed to send file. It might be inaccessible.", show_alert=True)


    async def show_season_menu(query, series_name: str):
        """Displays buttons for all unique seasons of the selected series."""
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
        
        # --- Single Column Season List (Matching Image) ---
        keyboard = []
        
        for season in seasons:
            callback_data = f'season_select|{series_name}|{season}'
            button_text = f"Season {season}" # Text format matching the image
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        # Add the 'Re Fetch Again' button and search buttons
        keyboard.append([InlineKeyboardButton("🚨 Re Fetch Again 🚨", callback_data='go_back_to_search')])
        
        # Mimic the 'Browse' and 'IMDb' buttons
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
            # NOTE: To fully replicate the image, you would send a photo here instead of editing text.
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass

    async def show_episode_menu(query, series_name: str, season: int):
        """Displays a menu with buttons for all episodes in the selected season (Matching Image 3)."""
        
        files = await get_all_files_by_season(series_name, season)
        
        if not files:
            await query.message.edit_text(text=f"❌ No episodes found for <b>{series_name}</b> Season {season:02d}.", parse_mode=enums.ParseMode.HTML)
            return

        keyboard = []
        
        for file_entry in files:
            channel_id = file_entry.get('channel_id')
            message_id = file_entry.get('message_id')
            
            # Use file_name to get the full episode title for the button text
            full_episode_name = file_entry.get('file_name', f"S{season:02d}E{file_entry.get('episode', 99)}") 
            
            # Simple button text to mimic the image (e.g., 'Season 01 Episode 01 - Winter Is Coming')
            # You might need to clean the file_name further here if it's too long/ugly
            
            # The button triggers sending the single file
            callback_data = f'send_single_episode|{channel_id}|{message_id}'
            button_text = full_episode_name[:60].strip() # Truncate for button size
            
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        # Add the final button to go back to the season list
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
