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

# --- NEW HELPER FOR FORCE SUBSCRIBE ---

async def check_force_subscribe(app: Client, user_id: int):
    """Checks if the user is a member of the required force subscribe channel."""
    channel_id = await get_global_setting("FORCE_SUB_CHANNEL_ID", None)
    
    if not channel_id:
        return True, None # Force sub is disabled
        
    try:
        # Check user's membership status
        member = await app.get_chat_member(channel_id, user_id)
        
        # Check if the member status is one of the valid statuses for a member
        if member.status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return True, None
        else:
            chat = await app.get_chat(channel_id)
            invite_link = chat.invite_link if chat.invite_link else f"https://t.me/{chat.username}" if chat.username else "N/A"
            return False, invite_link
            
    except Exception as e:
        logger.error(f"Force Sub check failed for channel {channel_id} and user {user_id}: {e}")
        # If bot cannot access the channel or other error, fallback to prompt the user to join
        try:
            chat = await app.get_chat(channel_id)
            invite_link = chat.invite_link if chat.invite_link else f"https://t.me/{chat.username}" if chat.username else "N/A"
            return False, invite_link
        except:
             return False, "https://t.me/telegram" # Default fallback link


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
        f"📺 Title **{series_name}** — Season **{season}**\n\n"
        f"Excellent choice!✨ {series_name} is a fantastic show. I hope, you will enjoy this TV Series\n\n"
        f"🌐 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗔𝘂𝗱𝗶𝗼 𝗟𝗮𝗻𝗴𝘂𝗮𝗴𝗲 👇:"

    )
    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        # Pass parse_mode to edit_caption
        await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in show_language_menu: {e}")
        try:
             # Fallback if the message wasn't a photo
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
    keyboard.append([InlineKeyboardButton(f"⬅️ Back", callback_data=f'bl|{series_name_short}|{season}')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    caption = (
        f"🎬 {series_name} – Season {season} ({language})\n\n"
        f"📌 Quality Selector\n\n"
        f"Tap a quality below —\n"
        f"▶️ Bot will instantly auto-send the WHOLE SEASON (all episodes) in that same resolution.\n\n"
        f"⚡ Lightning Fast\n"
        f"📦 Full Season Pack\n"
        f"🎥 Download And Watch:"
    )
    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        # Pass parse_mode to edit_caption
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


    @app.on_message(filters.text & filters.private & ~filters.command(["start", "imdb", "autodel", "forcesub"])) # <--- ADD forcesub
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
            # Reply directly if no series found
            await message.reply_text(f"❌ No series found `<code>{query}</code>`", parse_mode=enums.ParseMode.HTML)
            return

        # Use the first series found, skipping complex matching logic
        series_to_display = clean_series_name(unique_names[0])

        # Skip IMDB data fetching and poster URL extraction. Use Default Poster.
        poster_url = DEFAULT_POSTER_URL
        
        summary = await get_series_summary(series_to_display)
        
        languages_list = ", ".join(summary.get('languages', []))
        seasons_count = len(summary.get('seasons', []))
        qualities_list = ", ".join(summary.get('qualities', []))
        
        response = (
            f"**○ Title:** {series_to_display}\n"
            f"**○ Language:** {languages_list}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )
        
        keyboard = []
        series_name_short = series_to_display[:MAX_CALLBACK_LENGTH]
        for season in summary.get('seasons', []):
            callback_data = f'ss|{series_name_short}|{season}'
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])
        
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        # --- CHANGE: Reply immediately with the final result photo/media ---
        try:
            sent_message = await message.reply_photo(
                photo=poster_url, 
                caption=response,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to reply with photo for search result: {e}. Falling back to reply_text.")
            # Fallback to plain text if photo sending fails
            sent_message = await message.reply_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)


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
        # Use try/except for type conversion for robustness
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number.", show_alert=True)
            return
            
        language = data[3]
        quality = data[4]

        await query.answer(f"Sending {quality}/{language} episodes for S{season}...", cache_time=0)

        # Assume get_all_files_by_season_and_quality is defined elsewhere
        files = await get_all_files_by_season_and_quality(series_name, season, quality, language)

        if not files:
            await query.answer("❌ No files found.", show_alert=True)
            return

        # Cache bot_username lookup outside the loop
        bot_username = (await app.get_me()).username
        sent_count = 0
        
        # Loop through all files to send
        for file_entry in files:
            channel_id = file_entry["channel_id"]
            message_id = file_entry["message_id"]
            # Use .get with a default for safer access
            file_name = file_entry.get("file_name", f"S{file_entry.get('season', 0)}E{file_entry.get('episode', 99)}")

            # Assume CUSTOM_CAPTION_TEXT is defined elsewhere
            custom_caption = CUSTOM_CAPTION_TEXT.format(
                file_name=file_name,
                bot_username=bot_username
            )

            try:
                file_id = file_entry.get("file_id")
                
                # Prefer sending by file_id if available, otherwise copy the message
                if file_id:
                    # 🎯 FIX: Use send_document if file_id is available (for faster sending/direct access)
                    await app.send_document(
                        chat_id=query.from_user.id,
                        document=file_id,
                        caption=custom_caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                else:
                    # 🎯 FIX: Copy the original message if file_id is not available
                    await app.copy_message(
                        chat_id=query.from_user.id,
                        from_chat_id=channel_id,
                        message_id=message_id,
                        caption=custom_caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                
                sent_count += 1 # Increment only on successful send/copy

            except Exception as e:
                # Assume logger is defined elsewhere
                logger.error(f"Copy/Send fail for {message_id} in {channel_id}: {e}")
                # Continue to the next file if one fails

        # Final success message logic is correct (moved outside the loop)
        success_text = (
            f"✅ Successfully sent {sent_count} files from "
            f"**{series_name}** — Season {season} ({quality})"
        )

        await app.send_message(
            chat_id=query.from_user.id,
            text=success_text,
            parse_mode=enums.ParseMode.MARKDOWN
        )
        
    @app.on_callback_query(filters.regex(r"^back_to_search\|"))
    async def back_to_search_handler(app, query):
        data = query.data.split('|')
        if len(data) < 2:
            await query.answer("❌ Error: Invalid back data.", show_alert=True)
            return
            
        series_name = data[1]
        imdb_data = None

        try:
            # We still fetch IMDB data here to get the poster when going back
            imdb_data = await asyncio.get_event_loop().run_in_executor(None, get_imdb_data, series_name)
        except:
            pass
        
        # Get poster URL for back action
        poster_url = imdb_data.get('poster') if imdb_data and imdb_data.get('poster') else DEFAULT_POSTER_URL
        
        summary = await get_series_summary(series_name)
        
        languages_list = ", ".join(summary.get('languages', []))
        seasons_count = len(summary.get('seasons', []))
        qualities_list = ", ".join(summary.get('qualities', []))
        
        response = (
            f"**○ Title:** {series_name}\n"
            f"**○ Language:** {languages_list}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )
        
        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary.get('seasons', []):
            callback_data = f'ss|{series_name_short}|{season}'
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        # --- FIX: Moved parse_mode inside InputMediaPhoto to resolve the error ---
        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                reply_markup=reply_markup
                # Removed the redundant 'parse_mode' argument here
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
                parse_mode=enums.ParseMode.HTML # Passed parse_mode to edit_caption
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
                    parse_mode=enums.ParseMode.HTML # Passed parse_mode to edit_text
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
            InlineKeyboardButton("⚙️Help", callback_data="help|"),
            InlineKeyboardButton("🤠About", callback_data="about|")
        ]])
        reply_markup = CYBER_KEYBOARD    
        try:
            # Edit the caption (since it should be a photo message)
            await query.message.edit_caption(
                home_text, 
                reply_markup=reply_markup, 
                parse_mode=enums.ParseMode.HTML # Passed parse_mode to edit_caption
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in cyber_menu_handler: {e}")
            # Fallback
            try:
                await query.message.edit_text(
                    home_text,
                    reply_markup=reply_markup, 
                    parse_mode=enums.ParseMode.HTML # Passed parse_mode to edit_text
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
                parse_mode=enums.ParseMode.HTML # Passed parse_mode to edit_caption
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
                    parse_mode=enums.ParseMode.HTML # Passed parse_mode to edit_text
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
