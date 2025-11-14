# groupfilter.py
"""
Group search / selection handlers.

Flow:
 - In a group message (a search result message posted by the bot), users tap a Season button (callback: gss|<series>|<season>)
 - Bot shows language menu in-place (callbacks starting with gls|)
 - Bot shows quality menu in-place (callbacks starting with gqs|)
 - When user taps a quality in the group, the bot:
     1) checks force-subscribe (using utils.check_force_subscribe)
     2) if not subscribed -> sends an in-group alert with a Join Channel button (invite link)
     3) if subscribed -> sends an initial DM to the user "Starting transfer..." and begins sending files to the user's private chat
"""

import asyncio
import logging
from pyrogram import filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import MessageNotModified

# Import project helpers (same names used across your handlers/menus/database)
from DB.database import (
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
    get_all_files_by_season_and_quality,
    get_series_summary,
)
from utils import check_force_subscribe, clean_series_name, delete_message_later
from config import (
    MAX_CALLBACK_LENGTH,
    CUSTOM_CAPTION_TEXT,
    OMDB_CACHE_DB,
    DEFAULT_POSTER,
    logger,
)
from .poster import get_series_poster_async


def register_group_handlers(cyber):
    """
    Register callback handlers for group-based in-message filtering.
    This mirrors the private-chat flow but sends the final files in a private chat
    after a user chooses Quality from the group message UI.
    """

    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def group_season_select(cyber_client, query: CallbackQuery):
        """
        group Season button clicked -> show languages for that season (in-group).
        Callback format: gss|<series_name_short>|<season>
        """
        data = query.data.split("|", 2)
        if len(data) < 3:
            await query.answer("Invalid selection.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("Invalid season number.", show_alert=True)
            return

        # Fetch languages for series/season
        try:
            languages = await get_unique_languages_by_season(series_name, season)
        except Exception as e:
            logger.error(f"[group_season_select] DB error for {series_name} S{season}: {e}")
            languages = []

        if not languages:
            await query.answer(f"No languages found for {series_name} S{season}.", show_alert=True)
            return

        kb = []
        series_short = series_name[:MAX_CALLBACK_LENGTH]
        for lang in languages:
            kb.append([InlineKeyboardButton(f"🔸 {lang} 🔸", callback_data=f"gls|{series_short}|{season}|{lang}")])

        kb.append([InlineKeyboardButton("↺ Back to Search", callback_data=f"gback|{series_short}")])
        reply_markup = InlineKeyboardMarkup(kb)

        caption = f"Select language for *{series_name}* — Season {season}"
        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode="markdown")
        except MessageNotModified:
            pass
        except Exception as e:
            logger.exception(f"[group_season_select] edit_caption failed: {e}")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode="markdown")
            except Exception:
                pass

        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def group_language_select(cyber_client, query: CallbackQuery):
        """
        group Language clicked -> show qualities for that season+language (in-group).
        Callback: gls|<series>|<season>|<lang>
        """
        data = query.data.split("|")
        if len(data) < 4:
            await query.answer("Invalid selection.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("Invalid season.", show_alert=True)
            return
        language = data[3]

        try:
            qualities = await get_unique_qualities_by_lang_season(series_name, season, language)
        except Exception as e:
            logger.error(f"[group_language_select] DB error for {series_name} S{season} {language}: {e}")
            qualities = []

        if not qualities:
            await query.answer(f"No qualities available for {language} in S{season}.", show_alert=True)
            return

        kb = []
        series_short = series_name[:MAX_CALLBACK_LENGTH]
        for q in qualities:
            # When clicked, this will trigger a private transfer for the user who clicked
            kb.append([InlineKeyboardButton(f"🔹 {q} 🔹", callback_data=f"gqs|{series_short}|{season}|{language}|{q}")])

        kb.append([InlineKeyboardButton("↺ Back to Seasons", callback_data=f"gback|{series_short}")])
        reply_markup = InlineKeyboardMarkup(kb)

        caption = f"Select quality for *{series_name}* — Season {season} ({language})"
        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode="markdown")
        except MessageNotModified:
            pass
        except Exception as e:
            logger.exception(f"[group_language_select] edit_caption failed: {e}")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode="markdown")
            except Exception:
                pass

        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def group_quality_select(cyber_client, query: CallbackQuery):
        """
        Quality clicked in group -> begin private transfer to the clicking user.
        Callback: gqs|<series>|<season>|<language>|<quality>
        """
        data = query.data.split("|")
        if len(data) < 5:
            await query.answer("Invalid selection.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("Invalid season.", show_alert=True)
            return
        language = data[3]
        quality = data[4]

        user = query.from_user
        user_id = user.id

        # Check force-subscribe
        try:
            is_member, invite_link = await check_force_subscribe(cyber_client, user_id)
        except Exception as e:
            logger.error(f"[group_quality_select] check_force_subscribe error: {e}")
            is_member, invite_link = False, None

        if not is_member:
            # In-group alert with Join button (invite_link may be None — fallback to generic text)
            kb = []
            if invite_link:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("📣 Join Channel", url=invite_link)]])
                await query.answer("You must join the channel before files can be sent.", show_alert=True)
                try:
                    await query.message.reply_text(
                        "🛑 You must join the required channel to receive files. Click below to join and try again.",
                        reply_markup=kb
                    )
                except Exception:
                    pass
            else:
                await query.answer("Please join the required channel to receive files.", show_alert=True)
            return

        # If subscribed, initiate private transfer:
        await query.answer("Preparing your files — check your private messages.", cache_time=0)

        # Send an initial DM so the user's PM with the bot is created (if they haven't pressed /start)
        try:
            await cyber_client.send_message(
                chat_id=user_id,
                text=f"Starting transfer for *{series_name}* — Season {season} ({language} / {quality})\nI'll send all available episodes now.",
                parse_mode="markdown"
            )
        except Exception as e:
            # If bot cannot message user (likely because user hasn't started bot), instruct them to start
            logger.warning(f"[group_quality_select] cannot DM user {user_id}: {e}")
            try:
                start_btn = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Start Bot", url=f"https://t.me/{(await cyber_client.get_me()).username}?start=1")]]
                )
            except Exception:
                start_btn = None

            try:
                await query.message.reply_text(
                    "I could not message you privately. Please open the bot in private and press *Start* to allow deliveries.",
                    reply_markup=start_btn,
                    parse_mode="markdown"
                )
            except Exception:
                pass
            return

        # Fetch files and send them in private
        try:
            files = await get_all_files_by_season_and_quality(series_name, season, quality, language)
        except Exception as e:
            logger.error(f"[group_quality_select] DB error fetching files: {e}")
            files = []

        if not files:
            try:
                await cyber_client.send_message(user_id, f"❌ No files found for *{series_name}* S{season} ({language}/{quality}).", parse_mode="markdown")
            except Exception:
                pass
            return

        bot_username = (await cyber_client.get_me()).username
        sent_count = 0

        for file_entry in files:
            channel_id = file_entry.get("channel_id")
            message_id = file_entry.get("message_id")
            file_id = file_entry.get("file_id")
            file_name = file_entry.get("file_name", f"S{file_entry.get('season', 0)}E{file_entry.get('episode', 99)}")
            caption = CUSTOM_CAPTION_TEXT.format(file_name=file_name, bot_username=bot_username)

            try:
                if file_id:
                    await cyber_client.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode="markdown")
                else:
                    # fallback to copy_message if no file_id
                    await cyber_client.copy_message(chat_id=user_id, from_chat_id=channel_id, message_id=message_id)
                sent_count += 1
            except Exception as e:
                logger.error(f"[group_quality_select] send/copy failed for {message_id}@{channel_id}: {e}")
                continue

            # throttle
            if sent_count < len(files):
                await asyncio.sleep(1.2)

        # Optionally notify in group that user's transfer is complete (non-intrusive)
        try:
            await query.message.reply_text(f"✅ Transfer started for {user.mention}. Files are being delivered in private.")
        except Exception:
            pass

    # Optional: back button handler in-group
    @cyber.on_callback_query(filters.regex(r"^gback\|"))
    async def group_back_handler(cyber_client, query: CallbackQuery):
        data = query.data.split("|", 1)
        if len(data) < 2:
            await query.answer()
            return
        series_name = data[1]
        # Use the same summary UI as private; show seasons
        try:
            summary = await get_series_summary(series_name)
        except Exception as e:
            logger.error(f"[group_back_handler] get_series_summary failed: {e}")
            await query.answer("Cannot fetch series information.", show_alert=True)
            return

        keyboard = []
        series_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary.get("seasons", []):
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=f"gss|{series_short}|{season}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        caption = f"Select Season for *{series_name}*"
        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode="markdown")
        except MessageNotModified:
            pass
        except Exception:
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode="markdown")
            except Exception:
                pass

        await query.answer()
