# menus.py
import asyncio
import shelve
from pyrogram import Client, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    CallbackQuery, # Added for type hinting
)
from pyrogram.errors import MessageNotModified
from config import (
    logger, 
    MAX_CALLBACK_LENGTH, 
    DEFAULT_POSTER, 
    ADMIN_USERS, 
    OMDB_CACHE_DB
)
from DB.database import (
    get_global_setting,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
    get_series_summary
)
from utils import delete_message_later
from .poster import get_series_poster_async

async def show_language_menu(cyber: Client, query: CallbackQuery, series_name: str, season: int):
    """Displays a menu for selecting the audio language for a specific season."""
    await query.answer("Fetching languages...", cache_time=0)
    
    languages = await get_unique_languages_by_season(series_name, season)
    if not languages:
        await query.answer(f"❌ No languages found for {series_name} S{season}.", show_alert=True)
        return

    keyboard = []
    # Ensure series_name fits within the callback data limit (MAX_CALLBACK_LENGTH)
    series_name_short = series_name[:MAX_CALLBACK_LENGTH]
    for lang in languages:
        # Callback: ls|series_name_short|season|lang
        callback_data = f"ls|{series_name_short}|{season}|{lang}"
        display_text = f"🔸 {lang} 🔸"
        keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])

    # Callback: back_to_search|series_name_short
    keyboard.append([InlineKeyboardButton("↺ Bᴀᴄᴋ Tᴏ Sᴇᴀʀᴄʜ ↻", callback_data=f"back_to_search|{series_name_short}")])
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
        # Fallback to edit_text if edit_caption fails
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception:
            pass

    if auto_delete_timer and int(auto_delete_timer) > 0:
        # Schedule message deletion
        asyncio.create_task(delete_message_later(cyber, query.message.chat.id, query.message.id, int(auto_delete_timer)))


async def show_quality_menu(cyber: Client, query: CallbackQuery, series_name: str, language: str, season: int):
    """Displays a menu for selecting the video quality for a specific season and language."""
    await query.answer("Fetching qualities...", cache_time=0)
    
    qualities = await get_unique_qualities_by_lang_season(series_name, language, season)
    if not qualities:
        await query.answer(f"❌ No quality files found for S{season} ({language}).", show_alert=True)
        return

    keyboard = []
    # Ensure series_name fits within the callback data limit
    series_name_short = series_name[:MAX_CALLBACK_LENGTH]
    for quality in qualities:
        # Callback: qs|series_name_short|season|language|quality
        callback_data = f"qs|{series_name_short}|{season}|{language}|{quality}"
        keyboard.append([InlineKeyboardButton(f"🔹 {quality} 🔹", callback_data=callback_data)])

    # Callback: bl|series_name_short|season
    keyboard.append([InlineKeyboardButton("↺ Bᴀᴄᴋ Tᴏ Lᴀɴɢᴜɢᴇs ↻", callback_data=f"bl|{series_name_short}|{season}")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    caption = (
        f"🎬 {series_name} – Season {season} ({language})\n\n"
        f"📌 Quality Selector\n\n"
        f"Tap a quality below —\n"
        f"▶️ Bot will instantly auto-send the **WHOLE SEASON** (all episodes) in that same resolution.\n\n"
        f">⚡ Lightning Fast\n"
        f">📦 Full Season Pack\n"
        f">🎥 Download And Watch:"
    )
    
    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in show_quality_menu: {e}")
        # Fallback to edit_text if edit_caption fails
        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception:
            pass

    if auto_delete_timer and int(auto_delete_timer) > 0:
        # Schedule message deletion
        asyncio.create_task(delete_message_later(cyber, query.message.chat.id, query.message.id, int(auto_delete_timer)))

# --- General Menu Handlers (Moved from conversation.py) ---

async def help_menu_handler(cyber: Client, query: CallbackQuery):
    """Displays the help menu."""
    help_text = (
        "<b>💡 Help & Instructions</b>\n\n"
        "1. <b>Search</b>: Simply send the <b>name</b> of the movie or series you are looking for.\n"
        "2. <b>Select</b>: Tap the <b>Season</b> button for a series.\n"
        "3. <b>Filter</b>: Choose the <b>Language</b> and then the <b>Quality</b>.\n"
        "4. <b>Receive</b>: The bot will automatically start sending the files to you.\n\n"
        "<i>If you see 'No series found', try a slightly different or shorter name.</i>"
    )

    HELP_KEYBOARD = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏡 ʜᴏᴍᴇ", callback_data="cyber|"),
         InlineKeyboardButton("🔅 ᴀᴅᴍɪɴ (ᴏɴʟʏ)", callback_data="admin_cmd|")]
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


async def cyber_menu_handler(cyber: Client, query: CallbackQuery):
    """Displays the main bot/home menu."""
    user = query.from_user
    bot = await cyber.get_me()
    user_mention = user.mention
    START_TEXT = (
        f"🖤 <b>Hey {user_mention}!</b>\n\n"
        f"Welcome to <b>{bot.first_name}</b> — your private cinema in chat 🎭\n"
        "Just type what you’re looking for and I’ll fetch it faster than light ⚡\n\n"
        "<blockquote>Let’s dive into the world of entertainment 🌙</blockquote>"
    )
    CYBER_KEYBOARD = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎞 ʙʀᴏᴡsᴇ ʟɪʙʀᴀʀʏ 🎞", switch_inline_query_current_chat="")
        ],[
            InlineKeyboardButton("⚡ ʜᴇʟᴘ", callback_data="help|menu"),
            InlineKeyboardButton("ᴀʙᴏᴜᴛ ✨", callback_data="about|menu")
        ],[
            InlineKeyboardButton("🍷 sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ 🍷", url="t.me/TomxSupport")
        ]
    ])
    reply_markup = CYBER_KEYBOARD
    try:
        await query.message.edit_media(
            media=InputMediaPhoto(media=DEFAULT_POSTER, caption=START_TEXT, parse_mode=enums.ParseMode.HTML),
            reply_markup=reply_markup,
        )
    except MessageNotModified:
        pass
    except Exception as e:
        # Fallback to edit_text if it fails to edit the media (e.g., if the original message was text)
        logger.error(f"Error editing message media/caption in cyber_menu_handler: {e}")
        try:
            await query.message.edit_text(
                START_TEXT,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass

    await query.answer("Back To Home")


async def admin_menu_handler(cyber: Client, query: CallbackQuery):
    """Displays the admin menu (restricted)."""
    user_id = query.from_user.id
    is_authorized = False
    try:
        # Check if user_id is in ADMIN_USERS (handling both list and single ID configurations)
        if isinstance(ADMIN_USERS, (list, tuple, set)):
            is_authorized = user_id in ADMIN_USERS
        else:
            is_authorized = (user_id == int(ADMIN_USERS))
    except Exception:
        is_authorized = False

    if not is_authorized:
        await query.answer("You are not authorized. Only the admin can open this menu.", show_alert=True)
        return

    admin_text = (
        f"**𝒯𝐻𝐼𝒮 𝒴𝒪𝒰𝑅 𝒜𝒟𝑀𝐼𝒩 𝒞𝒪𝑀𝑀𝒜𝒩𝒟𝒮**\n\n"
        f"/setskip - To Skip Indexing File range\n"
        f"/broadcast - To broadcast Message To users\n"
        f"/deleteall - Delete All Files From Database\n"
        f"/deletefile  - Delete a replyed File from database\n"
        f"/total - Get Total Uses and Files\n"
        f"/imdb - To Turn on and off Imdb\n"
        f"/fsub - To Set Force Subscribe\n"
        f"/autodel - To Set Autodelete Time\n"
    )
    ADMIN_KEYBOARD = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏡 ʜᴏᴍᴇ", callback_data="cyber|"),
         InlineKeyboardButton("🚫 ᴄʟᴏsᴇ", callback_data="close")]
    ])
    reply_markup = ADMIN_KEYBOARD
    try:
        await query.message.edit_caption(
            admin_text,
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

    await query.answer("Showing admin information.")


async def about_menu_handler(cyber: Client, query: CallbackQuery):
    """Displays the about menu."""
    about_text = (
        f"<blockquote>‣ ᴍʏ ɴᴀᴍᴇ : <a href='https://t.me/TGxSeriesBot'>Sᴇʀɪᴇs Bᴏᴛ</a>\n"
        f"‣ ᴄʀᴇᴀᴛᴏʀ : <a href='tg://settings'>ᴛʜɪs ᴘᴇʀsᴏɴ</a>\n"
        f"‣ ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href='https://t.me/CyberTGx'>ᴄʏʙᴇʀ ᠰ ᴛɢ</a>\n"
        f"‣ ʟɪʙʀᴀʀʏ : <a href='https://docs.pyrogram.org/'>ᴘʏʀᴏɢʀᴀᴍ</a>\n"
        f"‣ ʟᴀɴɢᴜᴀɢᴇ : <a href='https://www.python.org/download/releases/3.0/'>ᴘʏᴛʜᴏɴ 3</a>\n"
        f"‣ ᴘʀɪᴍᴀʀʏ ᴅʙ : <a href='https://www.mongodb.com/'>ᴍᴏɴɢᴏ ᴅʙ</a>\n"
        f"‣ sᴇᴄᴏɴᴅᴀʀʏ ᴅʙ : <a href='https://www.freesqldatabase.com/'>sᴏ̨ʟ ᴅʙ</a>\n"
        f"‣ ʙᴏᴛ sᴇʀᴠᴇʀ : <a href='https://t.me/quickfastt'>ǫᴜɪᴄᴋ ꜰᴀsᴛ</a></b></blockquote>"
    )
    ABOUT_KEYBOARD = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("💻 Source Code", url="https://github.com/CyberTG/SeriesFilterBot")
    ],[
        InlineKeyboardButton("🏡 ʜᴏᴍᴇ", callback_data="cyber|home"),
        InlineKeyboardButton("🕵‍♂ ʜᴇʟᴘ", callback_data="help|menu")
    ]])
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


async def close_menu_handler(_, query: CallbackQuery):
    """Deletes the message."""
    try:
        await query.message.delete()
        await query.answer("Closed.")
    except Exception:
        await query.answer("Can't close.")

# --- Back Handlers (Kept here as they are menu-related) ---

async def back_to_language_handler(cyber: Client, query: CallbackQuery):
    """Handles going back to the language selection menu."""
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

    await show_language_menu(cyber, query, series_name, season)
    await query.answer()


async def back_to_search_handler(cyber: Client, query: CallbackQuery):
    """Handles going back to the main search result (season selection) menu."""
    data = query.data.split("|")
    if len(data) < 2:
        await query.answer("❌ Error: Invalid back data.", show_alert=True)
        return

    series_name = data[1]

    poster_url = DEFAULT_POSTER
    imdb_enabled = True
    try:
        imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
    except Exception:
        pass

    # Check Cache for Poster
    try:
        cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
        with shelve.open(cache_file) as db:
            key = f"poster::{series_name.strip().lower()}"
            if key in db:
                poster_url = db[key]
    except Exception:
        pass

    # Fetch Poster if needed
    if (not poster_url or poster_url == DEFAULT_POSTER) and imdb_enabled:
        try:
            poster_tmp = await get_series_poster_async(series_name)
            poster_url = poster_tmp or DEFAULT_POSTER
        except Exception as e:
            logger.warning(f"[OMDB FETCH FAIL - back_to_search] {series_name} -> {e}")
            poster_url = DEFAULT_POSTER

    summary = await get_series_summary(series_name)
    languages_list = ", ".join(summary.get("languages", []))
    seasons_count = len(summary.get("seasons", []))
    qualities_list = ", ".join(summary.get("qualities", []))

    response = (
        f"**○ Title:** {series_name}\n"
        f"**○ Language:** {languages_list or 'N/A'}\n"
        f"**○ Season Available:** {seasons_count}\n"
        f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
        f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
    )

    keyboard = []
    series_name_short = series_name[:MAX_CALLBACK_LENGTH]
    for season in summary.get("seasons", []):
        callback_data = f"ss|{series_name_short}|{season}"
        keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])

    reply_markup = InlineKeyboardMarkup(keyboard)
    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

    try:
        await query.message.edit_media(
            media=InputMediaPhoto(media=poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
            reply_markup=reply_markup,
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
        asyncio.create_task(delete_message_later(cyber, query.message.chat.id, query.message.id, int(auto_delete_timer)))

    await query.answer()
