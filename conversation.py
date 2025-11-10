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
# Assuming DEFAULT_POSTER_URL is available in config
from config import logger, CUSTOM_CAPTION_TEXT, INDEX_CHANNELS, DEFAULT_POSTER, ABOUT_TEXT, ADMIN_USERS, ADMIN_TEXT
from utils import clean_series_name, save_series_file_for_bulk_index
from poster import get_series_poster_async

# --- NEW: Spell Checker Import and Initialization ---
from spellchecker import SpellChecker

# Initialize SpellChecker with English dictionary
SPELL = SpellChecker() 
# --- END NEW ---

MAX_CALLBACK_LENGTH = 50

# --- Helper Functions ---

async def delete_message_later(app: Client, chat_id, message_id, delay):
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception:
        pass

async def _normalize_channel_id(val):
    """Convert channel id setting to int if possible."""
    if val is None:
        return None
    try:
        # allow strings like "-100123..." or numbers
        return int(val)
    except Exception:
        # could be @username or something else
        return val

# --- Force Subscribe Helper ---

async def check_force_subscribe(app: Client, user_id: int):
    """Checks if the user is a member of the required force subscribe channel."""
    channel_id_raw = await get_global_setting("FORCE_SUB_CHANNEL_ID", None)
    if not channel_id_raw:
        return True, None  # Force sub is disabled

    channel_id = await _normalize_channel_id(channel_id_raw)

    try:
        member = await app.get_chat_member(channel_id, user_id)
        if member.status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return True, None
        else:
            chat = await app.get_chat(channel_id)
            invite_link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else "N/A")
            return False, invite_link

    except Exception as e:
        logger.error(f"Force Sub check failed for channel {channel_id_raw} and user {user_id}: {e}")
        try:
            chat = await app.get_chat(channel_id)
            invite_link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else "N/A")
            return False, invite_link
        except Exception:
            return False, "https://t.me/telegram"  # Default fallback link

# --- Menus ---

async def show_language_menu(app: Client, query, series_name: str, season: int):
    languages = await get_unique_languages_by_season(series_name, season)
    if not languages:
        await query.answer(f"❌ No languages found for {series_name} S{season}.", show_alert=True)
        return

    keyboard = []
    series_name_short = series_name[:MAX_CALLBACK_LENGTH]
    for lang in languages:
        callback_data = f'ls|{series_name_short}|{season}|{lang}'
        display_text = f"🗣️ {lang}"
        keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])

    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f'back_to_search|{series_name_short}')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    caption = (
        f"📺 Title **{series_name}** — Season **{season}**\n\n"
        f"Excellent choice!✨ {series_name} is a fantastic show. I hope, you will enjoy this TV Series\n\n"
        f"🌐 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗔𝘂𝗱𝗶𝗼 𝗟𝗮𝗻𝗴𝘂𝗮𝗴𝗲 👇:"
    )
    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in show_language_menu: {e}")
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception:
            pass

    if auto_delete_timer and int(auto_delete_timer) > 0:
        asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, int(auto_delete_timer)))

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

    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f'bl|{series_name_short}|{season}')])
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
        await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in show_quality_menu: {e}")
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception:
            pass

    if auto_delete_timer and int(auto_delete_timer) > 0:
        asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, int(auto_delete_timer)))

# --- Handler Registration ---

def register_conversation_handlers(app: Client):

    @app.on_message((filters.document | filters.video | filters.audio) & filters.channel)
    async def media_handler(_, message: Message):
        if message.chat.id in INDEX_CHANNELS and message.media:
            saved, count = await save_series_file_for_bulk_index(message)
            if saved:
                logger.info(f"RTI: Indexed {count} new file(s) from channel {message.chat.id}")

    @app.on_message(filters.text & filters.private & ~filters.command(["start", "imdb", "autodel", "forcesub"]))
    async def search_handler(app, message: Message):
        
        query = message.text.strip()
        if query.startswith('/') and query.lower() not in ["/index"]:
            return

        # Force Subscribe
        is_member, invite_link = await check_force_subscribe(app, message.from_user.id)
        if not is_member:
            await message.reply_text(
                "🛑 **Please join our channel to get search results.**\n\n"
                "Click the button below to join:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 Join Channel", url=invite_link)]]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            return

        # --- NEW: Spell Check Logic ---
        # 1. Get the corrected query
        corrected_query = " ".join(SPELL.correction(word) for word in query.split())
        
        # 2. Try searching with the corrected query first
        unique_names = await search_series(corrected_query)
        
        # Determine if a suggestion is necessary
        is_suggestion = (query.lower() != corrected_query.lower()) and unique_names
        
        # 3. If no results found with corrected query, fall back to the original query
        if not unique_names and not is_suggestion:
            unique_names = await search_series(query)
        # --- END NEW: Spell Check Logic ---


        if not unique_names:
            await message.reply_text(
                f"❌ No series found `<code>{query}</code>`",
                parse_mode=enums.ParseMode.HTML
            )
            return

        # Pick first
        series_to_display = clean_series_name(unique_names[0])

        # Always use default poster (IMDb disabled by design)
        poster_url = DEFAULT_POSTER

        summary = await get_series_summary(series_to_display)
        languages_list = ", ".join(summary.get('languages', []))
        seasons_count = len(summary.get('seasons', []))
        qualities_list = ", ".join(summary.get('qualities', []))

        # --- NEW: Response based on spell check ---
        if is_suggestion:
            response_prefix = (
                f"⚠️ **Did you mean: {corrected_query}?**\n\n"
            )
        else:
            response_prefix = ""
            
        response = (
            f"{response_prefix}"
            f"**○ Title:** {series_to_display}\n"
            f"**○ Language:** {languages_list}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )
        # --- END NEW: Response based on spell check ---

        keyboard = []
        series_name_short = series_to_display[:MAX_CALLBACK_LENGTH]
        for season in summary.get('seasons', []):
            callback_data = f'ss|{series_name_short}|{season}'
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])
            
        # --- NEW: Add a button to search the original query if a suggestion was made ---
        if is_suggestion:
             keyboard.append([InlineKeyboardButton(f"🔎 Search Original: {query}", callback_data=f'search_original|{query}')])
        # --- END NEW ---

        reply_markup = InlineKeyboardMarkup(keyboard)
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        try:
            sent_message = await message.reply_photo(
                photo=poster_url,
                caption=response,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to reply with photo for search result: {e}. Falling back to reply_text.")
            sent_message = await message.reply_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)

        if auto_delete_timer and int(auto_delete_timer) > 0:
            asyncio.create_task(delete_message_later(app, sent_message.chat.id, sent_message.id, int(auto_delete_timer)))

    # --- NEW: Handler for the "Search Original" button ---
    @app.on_callback_query(filters.regex(r"^search_original\|"))
    async def search_original_handler(app, query):
        data = query.data.split("|")
        if len(data) < 2:
            await query.answer("❌ Error: Invalid original search data.", show_alert=True)
            return
            
        original_query = data[1]
        await query.answer(f"Searching for '{original_query}'...", cache_time=0)
        
        # Search using the original, uncorrected query
        unique_names = await search_series(original_query)
        
        if not unique_names:
            # Edit the message to show no results for the original query
            await query.message.edit_caption(
                f"❌ No series found `<code>{original_query}</code>`",
                reply_markup=InlineKeyboardMarkup([]),
                parse_mode=enums.ParseMode.HTML
            )
            return

        series_to_display = clean_series_name(unique_names[0])
        poster_url = await get_series_poster_async(series_to_display)
        poster_url = poster_url or DEFAULT_POSTER

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

        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                reply_markup=reply_markup
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Failed to edit media for search_original_handler: {e}. Falling back to edit_text.")
            try:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass
        
    # --- END NEW: Handler for the "Search Original" button ---

    # Season select
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

    # Language select
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

    # Back to language from quality
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

        await show_language_menu(app, query, series_name, season)
        await query.answer()

    # Quality select
    @app.on_callback_query(filters.regex(r"^qs\|"))
    async def quality_select_handler(app, query):
        data = query.data.split("|")
        if len(data) < 5:
            await query.answer("❌ Error: Invalid quality selection data.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number.", show_alert=True)
            return

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
            file_name = file_entry.get("file_name", f"S{file_entry.get('season', 0)}E{file_entry.get('episode', 99)}")

            custom_caption = CUSTOM_CAPTION_TEXT.format(file_name=file_name, bot_username=bot_username)

            try:
                file_id = file_entry.get("file_id")
                if file_id:
                    await app.send_document(
                        chat_id=query.from_user.id,
                        document=file_id,
                        caption=custom_caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                else:
                    await app.copy_message(
                        chat_id=query.from_user.id,
                        from_chat_id=channel_id,
                        message_id=message_id,
                        caption=custom_caption,
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                sent_count += 1
            except Exception as e:
                logger.error(f"Copy/Send fail for {message_id} in {channel_id}: {e}")
                continue

        success_text = (
            f"✅ Successfully sent {sent_count} files from "
            f"**{series_name}** — Season {season} ({quality})"
        )
        await app.send_message(chat_id=query.from_user.id, text=success_text, parse_mode=enums.ParseMode.MARKDOWN)

    # File select (single)
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

    # Back to search (main)
    @app.on_callback_query(filters.regex(r"^back_to_search\|"))
    async def back_to_search_handler(app, query):
        data = query.data.split('|')
        if len(data) < 2:
            await query.answer("❌ Error: Invalid back data.", show_alert=True)
            return

        series_name = data[1]
        poster_url = DEFAULT_POSTER

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

        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                reply_markup=reply_markup
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Failed to edit media for back_to_search: {e}. Falling back to edit_text.")
            try:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass

        if auto_delete_timer and int(auto_delete_timer) > 0:
            asyncio.create_task(delete_message_later(app, query.message.chat.id, query.message.id, int(auto_delete_timer)))

        await query.answer()

    # Help
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

        HELP_KEYBOARD = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏡 Home", callback_data="cyber|"),
             InlineKeyboardButton("🕹 Admins", callback_data="admin_cmd|")]
        ])
        reply_markup = HELP_KEYBOARD

        try:
            await query.message.edit_caption(
                help_text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in help_menu_handler: {e}")
            try:
                await query.message.edit_text(
                    help_text,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass

        await query.answer("Showing help menu.")

    # Home
    @app.on_callback_query(filters.regex(r"^cyber\|"))
    async def cyber_menu_handler(app, query):
        home_text = (
            "<b>👋 Hey Bro</b>\n\n"
            "<b>I Am Your Series-Filter Bot</b> - advanced, powerful and designed to make your group a Series Request Group! You Can Also Search Series Here\n\n"
            "<b>Let's get started! 🚀</b>"
        )
        CYBER_KEYBOARD = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️Help", callback_data="help|"),
             InlineKeyboardButton("🤠About", callback_data="about|")]
        ])
        reply_markup = CYBER_KEYBOARD
        try:
            await query.message.edit_caption(
                home_text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in cyber_menu_handler: {e}")
            try:
                await query.message.edit_text(
                    home_text,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass

        await query.answer("Back To Home")

    # Admin menu (supports list or single int)
    @app.on_callback_query(filters.regex(r"^admin_cmd\|"))
    async def admin_menu_handler(app, query):
        user_id = query.from_user.id
        is_authorized = False
        try:
            if isinstance(ADMIN_USERS, (list, tuple, set)):
                is_authorized = user_id in ADMIN_USERS
            else:
                is_authorized = (user_id == int(ADMIN_USERS))
        except Exception:
            is_authorized = False

        if not is_authorized:
            await query.answer("You are not authorized. Only the admin can open this menu.", show_alert=True)
            return

        about_text = ADMIN_TEXT
        ABOUT_KEYBOARD = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="cyber|"),
             InlineKeyboardButton("Close", callback_data="close")]
        ])
        reply_markup = ABOUT_KEYBOARD
        try:
            await query.message.edit_caption(
                about_text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in admin_menu_handler: {e}")
            try:
                await query.message.edit_text(
                    about_text,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass

        await query.answer("Showing about information.")

    # About
    @app.on_callback_query(filters.regex(r"^about\|"))
    async def about_menu_handler(app, query):
        about_text = ABOUT_TEXT
        ABOUT_KEYBOARD = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Home", callback_data="cyber|"),
             InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/CyberTGx")]
        ])
        reply_markup = ABOUT_KEYBOARD
        try:
            await query.message.edit_caption(
                about_text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in about_menu_handler: {e}")
            try:
                await query.message.edit_text(
                    about_text,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass

        await query.answer("Showing about information.")

    @app.on_callback_query(filters.regex(r"close"))
    async def close_menu_handler(_, query):
        try:
            await query.message.delete()
            await query.answer("Closed.")
        except Exception:
            await query.answer("Can't close.")

    return [
        media_handler, search_handler, season_select_handler, language_select_handler,
        back_to_language_handler, quality_select_handler, file_select_handler,
        back_to_search_handler, help_menu_handler, about_menu_handler,
        admin_menu_handler, cyber_menu_handler, close_menu_handler,
        search_original_handler,
    ]
