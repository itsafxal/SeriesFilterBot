# menus.py
import asyncio
from pyrogram import Client, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified
from config import logger, MAX_CALLBACK_LENGTH
from DB.database import (
    get_global_setting,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)
from utils import delete_message_later


async def show_language_menu(cyber: Client, query, series_name: str, season: int):
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
        display_text = f"🪶 {lang} 🪶"
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


async def show_quality_menu(cyber: Client, query, series_name: str, language: str, season: int):
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
