import asyncio
import re
import requests
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto, # Added for editing message to include photo
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
# Assuming DEFAULT_POSTER_URL is available in config
from config import logger, CUSTOM_CAPTION_TEXT, INDEX_CHANNELS, DEFAULT_POSTER_URL, ABOUT_TEXT
from utils import clean_series_name, save_series_file_for_bulk_index
from movie_fetcher import get_imdb_data # Kept for get_imdb_data usage (for link and data, not poster)

MAX_CALLBACK_LENGTH = 50

# --- Helper Functions ---

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
        # Callback for selecting a language to go to quality menu
        callback_data = f'ls|{series_name_short}|{season}|{lang}'
        display_text = f"🗣️ {lang}"
        keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])
    
    # Callback for going back to the Season selection menu
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f'back_to_search|{series_name_short}')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = (
        f"🎬 **{series_name} - S{season}**\n"
        f"Select the **Language**:"
    )

    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        # CHANGE 1: Use edit_text if it's a photo message, as edit_text on a photo edits the caption
        # If it's a plain text message, edit_text works as expected.
        await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in show_language_menu: {e}")
        try:
             # Fallback if the message wasn't a photo (e.g., initial search message was only text)
             await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
             pass
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

    # FIX: Changed back button callback prefix to 'bl|' (Back to Language) 
    # This prevents the previous handler from looping and ensures a correct step back.
    keyboard.append([InlineKeyboardButton(f"⬅️ Back", callback_data=f'bl|{series_name_short}|{season}')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    caption = (
        f"🎬 **{series_name} - S{season}** ({language})\n"
        f"Select the desired **video quality**\n\n"
        f"▶️ After click — bot will auto-send all episodes"
    )

    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        # CHANGE 2: Use edit_caption for consistency, as the message should now be a photo
        await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in show_quality_menu: {e}")
        try:
             # Fallback
             await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
             pass
        except Exception:
             pass

    if auto_delete_timer > 0:
        asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer))


# --- Handler Registration ---

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
        try:
            imdb_data = await asyncio.get_event_loop().run_in_executor(None, get_imdb_data, series_to_display)
        except:
            pass

        imdb_title = imdb_data.get('title') if imdb_data else series_to_display
        imdb_released = imdb_data.get('year', 'N/A') if imdb_data else 'N/A'
        imdb_genre = imdb_data.get('genres', 'N/A') if imdb_data else 'N/A'
        imdb_rating = imdb_data.get('rating', 'N/A') if imdb_data else 'N/A'
        imdb_link = imdb_data.get('link') if imdb_data else None
        
        # CHANGE 3: Get poster URL
        poster_url = imdb_data.get('poster') if imdb_data and imdb_data.get('poster') else DEFAULT_POSTER_URL
        
        summary = await get_series_summary(series_to_display)
        
        languages_list = ", ".join(summary.get('languages', []))
        seasons_count = len(summary.get('seasons', []))
        qualities_list = ", ".join(summary.get('qualities', []))
        
        response = (
            f"**○ Title:** {imdb_title}\n"
            f"**○ Released On:** {imdb_released}\n"
            f"**○ Genre:** {imdb_genre}\n"
            f"**○ Rating:** {imdb_rating}\n"
            f"**○ Language:** {languages_list}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list}\n\n"
            f"**Select the --Season-- to continue:**"
        )
        
        keyboard = []
        series_name_short = series_to_display[:MAX_CALLBACK_LENGTH]
        for season in summary.get('seasons', []):
            callback_data = f'ss|{series_name_short}|{season}'
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])
        
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        # CHANGE 4: Change message from text to photo/media
        # Use edit_media to switch from a text message to a photo message
        try:
            sent_message = await initial_message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=response),
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Failed to edit media for search result: {e}. Falling back to edit_text.")
            # Fallback to plain text if photo editing fails
            sent_message = await initial_message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)


        if auto_delete_timer > 0:
            asyncio.create_task(delete_message_later(app, sent_message.chat.id, sent_message.id, auto_delete_timer))


    # FIX: Corrected regex filter to r"^ss\|" 
    @app.on_callback_query(filters.regex(r"^ss\|"))
    async def season_select_handler(app, query):
        
        data = query.data.split("|")
                
        if len(data) < 3:
            logger.error(f"Malformed callback data for season_select_handler: {query.data}")
            await query.answer("❌ Error: Invalid season selection data. Please try searching again.", show_alert=True)
            return
            
        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            logger.error(f"Callback data had non-integer season: {data[2]}")
            await query.answer("❌ Error: Invalid season number format.", show_alert=True)
            return

        await show_language_menu(app, query, series_name, season)
        await query.answer()


    # FIX: Corrected regex filter to r"^ls\|"
    @app.on_callback_query(filters.regex(r"^ls\|"))
    async def language_select_handler(app, query):
        data = query.data.split("|")
        if len(data) < 4:
            await query.answer("❌ Error: Invalid language selection data.", show_alert=True)
            return
            
        series_name = data[1]
        season = int(data[2])
        language = data[3]
        await show_quality_menu(app, query, series_name, language, season)
        await query.answer()

    
    # NEW HANDLER: Handles the 'bl|' callback (Back to Language) from the Quality Menu
    @app.on_callback_query(filters.regex(r"^bl\|"))
    async def back_to_language_handler(app, query):
        data = query.data.split("|")
        
        if len(data) < 3:
            await query.answer("❌ Error: Invalid back data.", show_alert=True)
            return
            
        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number format.", show_alert=True)
            return

        # Correctly calls show_language_menu to display the previous step
        await show_language_menu(app, query, series_name, season)
        await query.answer()


    # FIX: Corrected regex filter to r"^qs\|"
    @app.on_callback_query(filters.regex(r"^qs\|"))
    async def quality_select_handler(app, query):
        data = query.data.split("|")
        if len(data) < 5:
            await query.answer("❌ Error: Invalid quality selection data.", show_alert=True)
            return
            
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


    # FIX: Corrected regex filter to r"^fs\|"
    @app.on_callback_query(filters.regex(r"^fs\|"))
    async def file_select_handler(app, query):
        data = query.data.split('|')
        if len(data) < 4:
            await query.answer("❌ Error: Invalid file selection data.", show_alert=True)
            return
            
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


    # FIX 2: Corrected regex filter to r"^back_to_search\|"
    @app.on_callback_query(filters.regex(r"^back_to_search\|"))
    async def back_to_search_handler(app, query):
        data = query.data.split('|')
        if len(data) < 2:
            await query.answer("❌ Error: Invalid back data.", show_alert=True)
            return
            
        series_name = data[1]
        imdb_data = None

        try:
            imdb_data = await asyncio.get_event_loop().run_in_executor(None, get_imdb_data, series_name)
        except:
            pass

        imdb_title = imdb_data.get('title') if imdb_data else series_name
        imdb_released = imdb_data.get('year', 'N/A') if imdb_data else 'N/A'
        imdb_genre = imdb_data.get('genres', 'N/A') if imdb_data else 'N/A'
        imdb_rating = imdb_data.get('rating', 'N/A') if imdb_data else 'N/A'
        imdb_link = imdb_data.get('link') if imdb_data else None
        
        # CHANGE 5: Get poster URL for back action
        poster_url = imdb_data.get('poster') if imdb_data and imdb_data.get('poster') else DEFAULT_POSTER_URL
        
        summary = await get_series_summary(series_name)
        
        languages_list = ", ".join(summary.get('languages', []))
        seasons_count = len(summary.get('seasons', []))
        qualities_list = ", ".join(summary.get('qualities', []))
        
        response = (
            f"**○ Title:** {imdb_title}\n"
            f"**○ Released On:** {imdb_released}\n"
            f"**○ Genre:** {imdb_genre}\n"
            f"**○ Rating:** {imdb_rating}\n"
            f"**○ Language:** {languages_list}\n"
            f"**○ Season:** {seasons_count}\n"
            f"**○ Quality:** {qualities_list}\n\n"
            f"**Select the --Season-- to continue:**"
        )
        
        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary.get('seasons', []):
            callback_data = f'ss|{series_name_short}|{season}'
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])
        

        reply_markup = InlineKeyboardMarkup(keyboard)

        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        # CHANGE 6: Edit message media back to the search result photo
        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=response),
                reply_markup=reply_markup
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Failed to edit media for back_to_search: {e}. Falling back to edit_text.")
            # Fallback to plain text if photo editing fails
            try:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except:
                pass

        if auto_delete_timer > 0:
            asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, auto_delete_timer))

        await query.answer()

    
    @app.on_callback_query(filters.regex(r"^help\|"))
    async def help_menu_handler(app, query):
        help_text = (
            "<b>💡 Help & Instructions</b>\n\n"
            "1. <b>Search</b>: Simply send the <b>name</b> of the movie or series you are looking for.\n"
            "2. <b>Select</b>: Tap the <b>Season</b> button for a series.\n"
            "3. <b>Filter</b>: Choose the <b>Language</b> and then the <b>Quality</b>.\n"
            "4. <b>Receive</b>: The bot will automatically start sending the files to you.\n\n"
            "<i>If you see 'No series found', try a slightly different or shorter name.</i>"
        )
        
        HELP_KEYBOARD = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🏡 Home", callback_data="cyber|"),
            InlineKeyboardButton("🕹 Admins", callback_data="admin_cmd|")  # New button
        ]])
        reply_markup = HELP_KEYBOARD
        
        try:
            # Edit the caption (since it should be a photo message)
            await query.message.edit_caption(
                help_text, 
                reply_markup=reply_markup, 
                parse_mode=enums.ParseMode.HTML # Changed to HTML
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in help_menu_handler: {e}")
            # Fallback to edit_text
            try:
                await query.message.edit_text(
                    help_text, 
                    reply_markup=reply_markup, 
                    parse_mode=enums.ParseMode.HTML # Changed to HTML
                )
            except:
                pass

        await query.answer("Showing help menu.")


    @app.on_callback_query(filters.regex(r"^cyber\|"))
    async def cyber_menu_handler(app, query):
        home_text = (
            "<b>👋 Hey Bro</b>\n\n"
            "<b>I Am Your Series-Filter Bot</b> - advanced, powerful and designed to make your group a Series Request Group! You Can Also Search Series Here\n\n"
            "<b>Let's get started! 🚀</b>"
        )
        CYBER_KEYBOARD = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("⚙️Help", callback_data="help"),
            InlineKeyboardButton("🤠About", callback_data="about")
        ]])
        reply_markup = CYBER_KEYBOARD   
        try:
            # Edit the caption (since it should be a photo message)
            await query.message.edit_caption(
                home_text, 
                reply_markup=reply_markup, 
                parse_mode=enums.ParseMode.HTML # Changed to HTML
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in cyber_menu_handler: {e}")
            # Fallback
            # FIX: Ensure we use the correct variable `home_text` for the fallback.
            try:
                await query.message.edit_text(
                    home_text,
                    reply_markup=reply_markup, 
                    parse_mode=enums.ParseMode.HTML # Changed to HTML
                )
            except:
                pass

        await query.answer("Back To Home")


    # --- NEW HANDLER: About Menu Handler ---
    @app.on_callback_query(filters.regex(r"^about\|"))
    async def about_menu_handler(app, query):
        # ABOUT_TEXT is assumed to be defined in HTML format now
        about_text = ABOUT_TEXT
        
        ABOUT_KEYBOARD = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🔙 Back to Home", callback_data="cyber|"),
            InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/CyberTGx")  # New button
        ]])
        reply_markup = ABOUT_KEYBOARD
        try:
            # Edit the caption (since it should be a photo message)
            await query.message.edit_caption(
                about_text, 
                reply_markup=reply_markup, 
                parse_mode=enums.ParseMode.HTML # Changed to HTML
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in about_menu_handler: {e}")
            # Fallback
            try:
                await query.message.edit_text(
                    about_text, 
                    reply_markup=reply_markup, 
                    parse_mode=enums.ParseMode.HTML # Changed to HTML
                )
            except:
                pass
            
        await query.answer("Showing about information.")


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
        cyber_menu_handler, close_menu_handler
    ]
