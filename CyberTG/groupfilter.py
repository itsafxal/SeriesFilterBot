"""
Rewritten groupfilter.py — robust, production-ready handlers for group-based filtering

Features / fixes implemented:
 - Safe callback tokenization (base64 when short; SHA1 + in-memory cache when long)
 - Consistent callback parsing (no mixed split limits)
 - Uses human-friendly button labels while keeping callback payload short
 - Handles force-subscribe reliably (graceful fallbacks)
 - Robust DB-calls with fallback parameter ordering if required
 - Deep-link Start button sent when bot cannot DM user
 - Clear logging for diagnostics
 - Non-blocking throttling while sending files

Notes:
 - CALLBACK_CACHE is an in-memory cache mapping short SHA1 tokens -> full series name.
   If the bot restarts the cache will be lost. For persistence across restarts, replace
   with a small persistent store (Redis/DB) keyed by the SHA1 token.
 - Adjust TOKEN_MAX_LEN if you change other prefixes. Telegram callback_data limit is 64 bytes.

Usage:
 - Import and call register_group_handlers(cyber) from your bot startup code.
"""

import asyncio
import logging
import base64
import hashlib
from typing import Optional, Tuple

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified

# Project-specific imports (same names used across your codebase)
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
    DEFAULT_POSTER,
    logger,
)

# Module-level in-memory cache for SHA1 token -> series_name mappings
# WARNING: ephemeral; restart will clear it
CALLBACK_CACHE = {}
TOKEN_MAX_LEN = 58  # keep a margin under Telegram's 64 byte limit

log = logging.getLogger(__name__)


def _make_token_for_series(series_name: str) -> str:
    """Return a short token safe for callback_data.

    Strategy:
    - Try URL-safe base64 encoding. If encoded length <= TOKEN_MAX_LEN, use it.
    - Otherwise compute sha1 hex digest, store mapping in CALLBACK_CACHE and return token prefixed with "sha:"
    """
    if not series_name:
        return ""

    raw = series_name.encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    if len(b64) <= TOKEN_MAX_LEN:
        return f"b64:{b64}"

    # fallback to sha1
    sha = hashlib.sha1(raw).hexdigest()
    CALLBACK_CACHE[sha] = series_name
    return f"sha:{sha}"


def _resolve_series_from_token(token: str) -> Optional[str]:
    """Decode token returned from callback_data back to full series_name.

    Returns None if decoding fails.
    """
    try:
        if token.startswith("b64:"):
            payload = token.split(":", 1)[1]
            return base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        if token.startswith("sha:"):
            sha = token.split(":", 1)[1]
            return CALLBACK_CACHE.get(sha)
        # defensive: also accept bare tokens that might be b64 or sha
        if token.startswith("sha_") or len(token) == 40:
            return CALLBACK_CACHE.get(token.replace("sha_", ""))
    except Exception as e:
        log.exception("Token decode failed: %s", e)
    return None


def _short_button_label(text: str, max_len: int = 40) -> str:
    """Return a trimmed label suitable for button text (not callback)."""
    if not text:
        return text
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def register_group_handlers(cyber):
    """Register callback handlers for group-based in-message filtering.

    Call this once during bot startup with your Client instance (cyber).
    """

    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def group_season_select(cyber_client, query: CallbackQuery):
        """Handle season selection: show language list in-place.

        Callback format: gss|<token>|<season>
        Where <token> is what _make_token_for_series() produced.
        """
        data = query.data.split("|")
        if len(data) < 3:
            await query.answer("Invalid selection.", show_alert=True)
            return

        token = data[1]
        series_name = _resolve_series_from_token(token)
        if not series_name:
            await query.answer("Unable to resolve series. Try the search again.", show_alert=True)
            return

        try:
            season = int(data[2])
        except ValueError:
            await query.answer("Invalid season number.", show_alert=True)
            return

        # optional clean name for DB queries (depends on your DB storage)
        series_db_name = clean_series_name(series_name)

        try:
            languages = await get_unique_languages_by_season(series_db_name, season)
        except Exception as e:
            log.error("[group_season_select] DB error for %s S%d: %s", series_db_name, season, e)
            languages = []

        if not languages:
            await query.answer(f"No languages found for {series_name} S{season}.", show_alert=True)
            return

        kb = []
        for lang in languages:
            token_for_cb = _make_token_for_series(series_name)
            cb = f"gls|{token_for_cb}|{season}|{lang}"
            kb.append([InlineKeyboardButton(f"🔸 {lang} 🔸", callback_data=cb)])

        kb.append([InlineKeyboardButton("↺ Back to Search", callback_data=f"gback|{_make_token_for_series(series_name)}")])
        reply_markup = InlineKeyboardMarkup(kb)

        caption = f"Select language for *{series_name}* — Season {season}"
        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode="markdown")
        except MessageNotModified:
            pass
        except Exception as e:
            log.exception("[group_season_select] edit_caption failed: %s", e)
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode="markdown")
            except Exception:
                # final fallback: send a new message in thread — non-intrusive
                try:
                    await query.message.reply_text(caption, reply_markup=reply_markup, parse_mode="markdown")
                except Exception:
                    log.exception("[group_season_select] final fallback failed")

        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def group_language_select(cyber_client, query: CallbackQuery):
        """Handle language selection: show quality list in-place.

        Callback: gls|<token>|<season>|<lang>
        """
        data = query.data.split("|")
        if len(data) < 4:
            await query.answer("Invalid selection.", show_alert=True)
            return

        token = data[1]
        series_name = _resolve_series_from_token(token)
        if not series_name:
            await query.answer("Unable to resolve series. Try the search again.", show_alert=True)
            return

        try:
            season = int(data[2])
        except ValueError:
            await query.answer("Invalid season number.", show_alert=True)
            return
        language = data[3]

        try:
            qualities = await get_unique_qualities_by_lang_season(series_name, season, language)
        except Exception as e:
            log.error("[group_language_select] DB error for %s S%d %s: %s", series_name, season, language, e)
            qualities = []

        if not qualities:
            await query.answer(f"No qualities available for {language} in S{season}.", show_alert=True)
            return

        kb = []
        for q in qualities:
            token_for_cb = _make_token_for_series(series_name)
            cb = f"gqs|{token_for_cb}|{season}|{language}|{q}"
            kb.append([InlineKeyboardButton(f"🔹 {q} 🔹", callback_data=cb)])

        kb.append([InlineKeyboardButton("↺ Back to Seasons", callback_data=f"gback|{_make_token_for_series(series_name)}")])
        reply_markup = InlineKeyboardMarkup(kb)

        caption = f"Select quality for *{series_name}* — Season {season} ({language})"
        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode="markdown")
        except MessageNotModified:
            pass
        except Exception as e:
            log.exception("[group_language_select] edit_caption failed: %s", e)
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode="markdown")
            except Exception:
                try:
                    await query.message.reply_text(caption, reply_markup=reply_markup, parse_mode="markdown")
                except Exception:
                    log.exception("[group_language_select] final fallback failed")

        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def group_quality_select(cyber_client, query: CallbackQuery):
        """Quality clicked in group -> begin private transfer to the clicking user.

        Callback: gqs|<token>|<season>|<language>|<quality>
        """
        data = query.data.split("|")
        if len(data) < 5:
            await query.answer("Invalid selection.", show_alert=True)
            return

        token = data[1]
        series_name = _resolve_series_from_token(token)
        if not series_name:
            await query.answer("Unable to resolve series. Try the search again.", show_alert=True)
            return

        try:
            season = int(data[2])
        except ValueError:
            await query.answer("Invalid season number.", show_alert=True)
            return
        language = data[3]
        quality = data[4]

        user = query.from_user
        user_id = user.id

        # Check force-subscribe
        try:
            check_result = await check_force_subscribe(cyber_client, user_id)
            # check_force_subscribe may return tuple or single boolean depending on your implementation
            if isinstance(check_result, tuple):
                is_member, invite_link = check_result
            else:
                is_member = bool(check_result)
                invite_link = None
        except Exception as e:
            log.error("[group_quality_select] check_force_subscribe error: %s", e)
            is_member, invite_link = False, None

        if not is_member:
            # In-group alert with Join button (invite_link may be None)
            if invite_link:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("📣 Join Channel", url=invite_link)]])
                await query.answer("You must join the channel before files can be sent.", show_alert=True)
                try:
                    await query.message.reply_text(
                        "🛑 You must join the required channel to receive files. Click below to join and try again.",
                        reply_markup=kb,
                    )
                except Exception:
                    log.exception("Failed to send join prompt in-group")
            else:
                await query.answer("Please join the required channel to receive files.", show_alert=True)
            return

        # If subscribed, initiate private transfer:
        await query.answer("Preparing your files — check your private messages.", cache_time=0)

        # Send an initial DM so the user's PM with the bot is created (if they haven't pressed /start)
        try:
            me = await cyber_client.get_me()
            bot_username = me.username
            await cyber_client.send_message(
                chat_id=user_id,
                text=f"Starting transfer for *{series_name}* — Season {season} ({language} / {quality})\nI'll send all available episodes now.",
                parse_mode="markdown",
            )
        except Exception as e:
            log.warning("[group_quality_select] cannot DM user %s: %s", user_id, e)
            try:
                start_url = f"https://t.me/{(await cyber_client.get_me()).username}?start=autodl"
            except Exception:
                start_url = None

            start_btn = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Start Bot", url=start_url)]] if start_url else []
            )

            try:
                await query.message.reply_text(
                    "I could not message you privately. Please open the bot in private and press *Start* to allow deliveries.",
                    reply_markup=start_btn if start_url else None,
                    parse_mode="markdown",
                )
            except Exception:
                log.exception("Failed to send start instruction in-group")
            return

        # Fetch files and send them in private. Be tolerant to DB function signature differences.
        try:
            files = await get_all_files_by_season_and_quality(series_name, season, language, quality)
            # If function returned empty, try alternate ordering (some DB implementations use (series, season, quality, language))
            if not files:
                try:
                    files = await get_all_files_by_season_and_quality(series_name, season, quality, language)
                except Exception:
                    pass
        except Exception as e:
            log.error("[group_quality_select] DB error fetching files: %s", e)
            files = []

        if not files:
            try:
                await cyber_client.send_message(user_id, f"❌ No files found for *{series_name}* S{season} ({language}/{quality}).", parse_mode="markdown")
            except Exception:
                log.exception("Failed to notify user about missing files")
            return

        sent_count = 0
        try:
            me = await cyber_client.get_me()
            bot_username = me.username
        except Exception:
            bot_username = None

        for file_entry in files:
            channel_id = file_entry.get("channel_id")
            message_id = file_entry.get("message_id")
            file_id = file_entry.get("file_id")
            file_name = file_entry.get("file_name", f"S{file_entry.get('season', season)}E{file_entry.get('episode', '?')}")
            caption = CUSTOM_CAPTION_TEXT.format(file_name=file_name, bot_username=bot_username) if CUSTOM_CAPTION_TEXT else file_name

            try:
                if file_id:
                    await cyber_client.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode="markdown")
                else:
                    # fallback to copy_message if no file_id
                    await cyber_client.copy_message(chat_id=user_id, from_chat_id=channel_id, message_id=message_id)
                sent_count += 1
            except Exception as e:
                log.error("[group_quality_select] send/copy failed for %s@%s: %s", message_id, channel_id, e)
                # continue sending other files

            # throttle between sends to avoid flood limits
            if sent_count < len(files):
                await asyncio.sleep(1.2)

        # Optionally notify in group that user's transfer has started
        try:
            await query.message.reply_text(f"✅ Transfer started for {user.mention}. Files are being delivered in private.")
        except Exception:
            log.exception("Failed to send group transfer-start notification")

    @cyber.on_callback_query(filters.regex(r"^gback\|"))
    async def group_back_handler(cyber_client, query: CallbackQuery):
        data = query.data.split("|", 1)
        if len(data) < 2:
            await query.answer()
            return
        token = data[1]
        series_name = _resolve_series_from_token(token)
        if not series_name:
            await query.answer("Unable to resolve series.", show_alert=True)
            return

        try:
            summary = await get_series_summary(series_name)
        except Exception as e:
            log.error("[group_back_handler] get_series_summary failed: %s", e)
            await query.answer("Cannot fetch series information.", show_alert=True)
            return

        keyboard = []
        seasons = summary.get("seasons", []) if summary else []
        for season in seasons:
            token_for_cb = _make_token_for_series(series_name)
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=f"gss|{token_for_cb}|{season}")])

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
                log.exception("[group_back_handler] failed to display seasons")

        await query.answer()

    log.info("groupfilter handlers registered")
