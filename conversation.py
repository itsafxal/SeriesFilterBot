# conversation.py (Key handlers modified)

import asyncio
import re
import requests
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
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
# DEFAULT_POSTER_URL is removed from config, so we remove the import as well
from config import logger, CUSTOM_CAPTION_TEXT, INDEX_CHANNELS, ABOUT_TEXT, ADMIN_USERS, ADMIN_TEXT
from utils import clean_series_name, save_series_file_for_bulk_index
from imdb import get_imdb_data
import difflib

SERIES_CACHE = []
SERIES_CACHE_LC = []
MAX_CALLBACK_LENGTH = 50

# ... (Helper functions like delete_message_later, check_force_subscribe, show_language_menu, show_quality_menu are unchanged)
# ...

def register_conversation_handlers(app: Client):

    @app.on_message((filters.document | filters.video | filters.audio) & filters.channel)
    async def media_handler(_, message: Message):
        if message.chat.id in INDEX_CHANNELS and message.media:
            saved, count = await save_series_file_for_bulk_index(message)
            if saved:
                logger.info(f"RTI: Indexed {count} new file(s) from channel {message.chat.id}")
                global SERIES_CACHE, SERIES_CACHE_LC
                SERIES_CACHE = await search_series("")
                SERIES_CACHE_LC = [x.lower() for x in SERIES_CACHE]




    @app.on_message(filters.text & filters.private & ~filters.command(["start", "imdb", "autodel", "forcesub"]))
    async def search_handler(app, message: Message):
        query = message.text.strip()
        if query.startswith('/') and query.lower() not in ["/index"]:
            return
            
        # --- FORCE SUBSCRIBE CHECK ---
        is_member, invite_link = await check_force_subscribe(app, message.from_user.id)
        
        if not is_member:
            await message.reply_text(
                "🛑 **Please join our channel to get search results.**\n\n"
                "Click the button below to join:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📣 Join Channel", url=invite_link)
                ]]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            return
        # --- END FORCE SUBSCRIBE CHECK ---

        unique_names = await search_series(query)

        if not unique_names:
            if SERIES_CACHE:
                suggestions = difflib.get_close_matches(query.lower(), SERIES_CACHE_LC, n=3, cutoff=0.55)
                if suggestions:
                    rows = []
                    for s in suggestions:
                        idx = SERIES_CACHE_LC.index(s)
                        real_name = SERIES_CACHE[idx]
                        rows.append([InlineKeyboardButton(f"🔍 {real_name}", callback_data=f"spellfix|{real_name}")])
                    
                    suggestions_markup = InlineKeyboardMarkup(rows)
                    
                    await message.reply_text(
                        f"❌ No exact match found for `<code>{query}</code>`\n\n**Maybe you meant?**",
                        reply_markup=suggestions_markup,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    return
            await message.reply_text(
                f"❌ No series found `<code>{query}</code>`",
                parse_mode=enums.ParseMode.HTML
            ) 
            return
        
        series_to_display = clean_series_name(unique_names[0])

        # --- MODIFIED: Image Content Fetching ---
        imdb_data = None
        poster_content = None # Stores the binary content of the poster
        
        imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
        
        if imdb_enabled:
            try:
                imdb_data = await asyncio.get_event_loop().run_in_executor(None, get_imdb_data, series_to_display)
            except Exception as e:
                logger.error(f"Failed to fetch IMDB data for {series_to_display}: {e}")
                
            # Check for binary content
            if imdb_data and imdb_data.get('poster_content'):
                poster_content = imdb_data.get('poster_content')

        # --- FIX: Extract IMDb details more robustly ---
        if imdb_data and not imdb_data.get('error'):
            imdb_rating = imdb_data.get('rating', 'N/A')
            imdb_plot = imdb_data.get('plot', 'Plot information not available.')
        else:
            imdb_rating = 'N/A'
            imdb_plot = 'Plot information not available.'
        # ---------------------------------------------------

        summary = await get_series_summary(series_to_display)
        
        languages_list = ", ".join(summary.get('languages', []))
        seasons_count = len(summary.get('seasons', []))
        qualities_list = ",glichen mit den originalen IMDb-Daten.
            imdb_data = await asyncio.get_event_loop().run_in_executor(None, get_imdb_data, series_name)
        except:
            pass
        
        # 🎯 Check for poster_content instead of poster_url
        if imdb_data and imdb_data.get('poster_content'):
             poster_content = imdb_data.get('poster_content')
    
        # --- FIX: Extract IMDb details more robustly ---
        if imdb_data and not imdb_data.get('error'):
            imdb_rating = imdb_data.get('rating', 'N/A')
            imdb_plot = imdb_data.get('plot', 'Plot information not available.')
        else:
            imdb_rating = 'N/A'
            imdb_plot = 'Plot information not available.'
        # ---------------------------------------------------
        
        summary = await get_series_summary(series_name)
        
        languages_list = ", ".join(summary.get('languages', []))
        seasons_count = len(summary.get('seasons', []))
        qualities_list = ", ".join(summary.get('qualities', []))
        
        response = (
            f"**○ Title:** {series_name}\n"
            f"**⭐ IMDb Rating:** {imdb_rating}\n"
            f"**○ Language:** {languages_list}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list}\n\n"
            f"**📖 Plot:** {imdb_plot}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )
        
        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary.get('seasons', []):
            callback_data = f'ss|{series_name_short}|{season}'
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        # --- MODIFIED: Conditional media editing based on poster_content ---
        if poster_content: # If binary content was fetched
            try:
                # Use the binary content directly
                await query.message.edit_media(
                    media=InputMediaPhoto(media=poster_content, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                    reply_markup=reply_markup
                )
            except MessageNotModified:
                pass
            except Exception as e:
                logger.error(f"Failed to edit media with content for back_to_search: {e}. Falling back to edit_text.")
                try:
                    # Fallback to text if editing media fails
                    await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
                except:
                    pass
        else: # No poster content, edit to text only
            try:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except MessageNotModified:
                pass
            except Exception as e:
                logger.error(f"Failed to edit_text for back_to_search (no poster): {e}.")


        if auto_delete_timer > 0:
            asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer))

        await query.answer()

    
    # ... (Other handlers like help_menu_handler, cyber_menu_handler, etc., are unchanged)
    # ...

    @app.on_callback_query(filters.regex(r"^spellfix\|"))
    async def spellfix_handler(app, query):
        series = query.data.split("|")[1]
        fake = query.message
        fake.text = series
        await search_handler(app, fake)
        await query.answer()
        
    @app.on_callback_query(filters.regex(r"close"))
    async def close_menu_handler(_, query):
        try:
            await query.message.delete()
            await query.answer("Closed.")
        except:
            await query.answer("Can't close.")

    return [
        media_handler, search_handler, season_select_handler, language_select_handler,
        back_to_language_handler, quality_select_handler, file_select_handler,
        back_to_search_handler, help_menu_handler, about_menu_handler,      
        admin_menu_handler, cyber_menu_handler, close_menu_handler,
        spellfix_handler
    ]
