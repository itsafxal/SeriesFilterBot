# groupfilter.py
"""
Group search + fuzzy suggestions + group -> PM deep-link flow.

Behavior:
 - OPTION A (auto-detect): any plain text in groups (not commands/links) triggers search.
 - If exact matches found -> show poster + Season buttons (gss|...)
 - If no exact matches -> clean the query, run fuzzy suggestion (rapidfuzz preferred)
   and show up to 3 suggestion buttons (sugg_select|<user_id>|<title>)
 - When a suggestion is chosen, the message is edited to the series summary and Season
   buttons are shown (gss|...). All subsequent callbacks use gss| -> gls| -> gqs|.
 - On quality selection (gqs|) the bot shows a single "Open Bot (PM)" deep link:
   https://t.me/<bot_username>?start=send_<base64(series|season|lang|quality)>
 - This file expects your project to expose the async DB functions and utilities:
   search_series, get_series_summary, get_unique_languages_by_season,
   get_unique_qualities_by_lang_season, get_all_files_by_season_and_quality (used in /start).
   And utilities: clean_series_name, delete_message_later, check_force_subscribe.
"""

import asyncio
import base64
import re
from urllib.parse import quote, unquote
import shelve
from typing import List, Tuple

# attempt to import rapidfuzz, fall back if not available (but user requested rapidfuzz)
try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz  # type: ignore
    _HAVE_RAPIDFUZZ = True
except Exception:
    _HAVE_RAPIDFUZZ = False

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import MessageNotModified

from config import (
    logger,
    DEFAULT_POSTER,
    MAX_CALLBACK_LENGTH,
    OMDB_CACHE_DB,
)

from utils import clean_series_name, delete_message_later, check_force_subscribe
from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from .poster import get_series_poster_async


# -------------------------
# Helpers
# -------------------------
async def _build_poster_for_series(series_name: str) -> str:
    """Return cached poster or fetch; fallback to DEFAULT_POSTER."""
    poster_url = DEFAULT_POSTER
    try:
        cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
        with shelve.open(cache_file) as cache:
            key = f"poster::{series_name.strip().lower()}"
            if key in cache:
                poster_url = cache[key]
    except Exception:
        pass

    try:
        poster_tmp = await get_series_poster_async(series_name)
        poster_url = poster_tmp or DEFAULT_POSTER
    except Exception as e:
        logger.warning(f"[POSTER FETCH FAIL] {series_name} -> {e}")
        poster_url = DEFAULT_POSTER

    return poster_url


def _encode_start_payload(series: str, season: int, language: str, quality: str) -> str:
    """Encode payload for deep-link: send_<base64url>"""
    raw = f"{series}|{season}|{language}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")
    return f"send_{quote(b64)}"


def _clean_user_query(q: str) -> str:
    """Apply same cleaning regex you use in PM to strip filler words before fuzzy search."""
    if not q:
        return ""
    cleaned = re.sub(
        r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\sme)?)|movie(s)?|new|latest|br((o|u)h?)*|^h(e|a)?(l)*(o)*|mal(ayalam)?|t(h)?amil|file|that|find|und(o)*|kit(t(i|y)?)?o(w)?|thar(u)?(o)*w?|kittum(o)*|aya(k)*(um(o)*)?|full\smovie|any(one)|with\ssubtitle(s)?)",
        "",
        q,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned


def _fuzzy_suggestions(query: str, candidates: List[str], limit: int = 5) -> List[Tuple[str, float]]:
    """
    Return a sorted list of (candidate, score) best matches to query.
    Uses rapidfuzz if available; otherwise a simple ratio scorer.
    """
    if not candidates:
        return []

    if _HAVE_RAPIDFUZZ:
        # Use rapidfuzz.process.extract with scorer ratio
        extracted = rf_process.extract(query, candidates, scorer=rf_fuzz.ratio, limit=limit)
        # rapidfuzz returns tuples: (choice, score, idx)
        # Convert to (choice, score)
        return [(item[0], float(item[1])) for item in extracted if item[1] > 30.0]
    else:
        # fallback simple score: normalized common subsequence length ratio
        def simple_score(a: str, b: str) -> float:
            a_l = a.lower()
            b_l = b.lower()
            # count longest common substring length / avg length
            # simple heuristic: number of equal characters in order
            i = j = 0
            matches = 0
            for ch in a_l:
                pos = b_l.find(ch, j)
                if pos != -1:
                    matches += 1
                    j = pos + 1
            denom = (len(a_l) + len(b_l)) / 2 or 1
            return (matches / denom) * 100.0

        scored = [(cand, simple_score(query, cand)) for cand in candidates]
        scored = [x for x in scored if x[1] > 25.0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]


# -------------------------
# Main registration function
# -------------------------
def register_groupfilter_handlers(cyber: Client):
    """
    Call this during startup: register_groupfilter_handlers(cyber)
    Handlers are defined using decorators that reference the 'cyber' client parameter.
    """

    # -------------------------
    # Group text search handler
    # -------------------------
    @cyber.on_message(filters.text & filters.group & ~filters.edited)
    async def group_search_handler(_, message: Message):
        text = (message.text or "").strip()
        if not text or len(text) < 2:
            return

        # ignore obvious non-search content
        if text.startswith("/") or text.startswith(".") or "http://" in text or "https://" in text:
            return

        # 1) try direct search
        try:
            unique_names = await search_series(text)
        except Exception as e:
            logger.warning(f"[GROUP SEARCH ERROR] search_series failed for `{text}`: {e}")
            unique_names = []

        if unique_names:
            # show first match as usual
            series_to_display = clean_series_name(unique_names[0])
            poster_url = await _build_poster_for_series(series_to_display)
            try:
                summary = await get_series_summary(series_to_display)
            except Exception as e:
                logger.warning(f"[GROUP SUMMARY ERROR] {series_to_display} -> {e}")
                summary = {"languages": [], "seasons": [], "qualities": []}

            seasons = summary.get("seasons", [])
            if not seasons:
                return

            # build season buttons (gss|)
            keyboard = []
            series_short = series_to_display[:MAX_CALLBACK_LENGTH]
            for season in seasons:
                keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=f"gss|{series_short}|{season}")])

            caption = (
                f"**{series_to_display}**\n\n"
                f"Available Seasons: {len(seasons)}\n\n"
                f"Select a season to continue:"
            )

            try:
                await message.reply_photo(
                    poster_url,
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            except Exception:
                await message.reply_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            return

        # 2) no exact results -> attempt cleaned + fuzzy suggestion flow (like PM)
        clean_query = _clean_user_query(text)
        suggestion_list = []
        if clean_query:
            try:
                suggestion_list = await search_series(clean_query)
            except Exception as e:
                logger.warning(f"[GROUP SEARCH ERROR] search_series failed for CLEANED query `{clean_query}`: {e}")
                suggestion_list = []

        # If cleaned direct results found, show them
        if suggestion_list:
            # behave like exact result
            series_to_display = clean_series_name(suggestion_list[0])
            poster_url = await _build_poster_for_series(series_to_display)
            try:
                summary = await get_series_summary(series_to_display)
            except Exception:
                summary = {"languages": [], "seasons": [], "qualities": []}

            seasons = summary.get("seasons", [])
            if not seasons:
                return

            keyboard = []
            series_short = series_to_display[:MAX_CALLBACK_LENGTH]
            for season in seasons:
                keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=f"gss|{series_short}|{season}")])

            caption = (
                f"**{series_to_display}**\n\n"
                f"Available Seasons: {len(seasons)}\n\n"
                f"Select a season to continue:"
            )

            try:
                await message.reply_photo(
                    poster_url,
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            except Exception:
                await message.reply_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            return

        # 3) Build fuzzy suggestions from DB
        # We need a candidate list. Use search_series with tokens or get all names:
        # For efficiency, call search_series with the original text but fetch a larger set
        # fallback: run search_series for the first word and expand
        candidates = []
        try:
            # Try a simple broad approach: search by words in query to get candidate pool
            parts = re.split(r"\s+", text)[:3]
            seen = []
            for p in parts:
                try:
                    rs = await search_series(p)
                except Exception:
                    rs = []
                for r in rs:
                    if r not in seen:
                        seen.append(r)
            candidates = seen
        except Exception:
            candidates = []

        # If still empty, try a global search for likely results by scanning small queries
        if not candidates:
            try:
                # attempt tiny substrings
                substrings = [text[:i] for i in range(len(text), max(len(text) - 4, 0), -1)]
                seen = []
                for s in substrings:
                    try:
                        rs = await search_series(s)
                    except Exception:
                        rs = []
                    for r in rs:
                        if r not in seen:
                            seen.append(r)
                candidates = seen
            except Exception:
                candidates = []

        # If we have no candidates from DB, bail out (don't spam group)
        if not candidates:
            # final fallback: tell user nothing found
            await message.reply_text(f"❌ No series found `<code>{text}</code>`", parse_mode=enums.ParseMode.HTML)
            return

        # Prepare list of cleaned candidate strings for fuzzy scoring
        cand_clean = [clean_series_name(c) for c in candidates]

        # Score
        scored = _fuzzy_suggestions(text, cand_clean, limit=10)
        if not scored:
            # nothing matched well enough
            await message.reply_text(f"❌ No series found `<code>{text}</code>`", parse_mode=enums.ParseMode.HTML)
            return

        # Take top 3 unique cleaned names
        suggestions = []
        for name, score in scored:
            if name not in suggestions:
                suggestions.append(name)
            if len(suggestions) >= 3:
                break

        if not suggestions:
            await message.reply_text(f"❌ No series found `<code>{text}</code>`", parse_mode=enums.ParseMode.HTML)
            return

        # Build suggestion buttons (sugg_select|<user_id>|<movie>)
        user_id = message.from_user.id if message.from_user else 0
        btn = [
            [InlineKeyboardButton(text=s, callback_data=f"sugg_select|{user_id}|{s}")]
            for s in suggestions
        ]
        btn.append([InlineKeyboardButton(text="Close", callback_data=f"close_suggestion|{user_id}")])

        sent_message = await message.reply(
            f"I couldn't find anything related to `<code>{text}</code>`.\nDid you mean any of these?",
            reply_markup=InlineKeyboardMarkup(btn),
            parse_mode=enums.ParseMode.HTML,
        )

        # auto delete if configured
        try:
            auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
            if auto_delete_timer and int(auto_delete_timer) > 0:
                asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, int(auto_delete_timer)))
        except Exception:
            pass

    # -----------------------------------------------------
    # Suggestion selection handler (user clicked a suggestion)
    # We will edit that suggestion message to the series summary and Season buttons (gss|)
    # -----------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^sugg_select\|"))
    async def sugg_select_handler(cyber, query: CallbackQuery):
        data = query.data.split("|", 2)
        if len(data) < 3:
            await query.answer("❌ Error: Invalid suggestion data.", show_alert=True)
            return

        series_name = data[2]
        await query.answer(f"Selected: {series_name}...", cache_time=0)

        poster_url = DEFAULT_POSTER
        try:
            cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
            with shelve.open(cache_file) as db:
                key = f"poster::{series_name.strip().lower()}"
                if key in db:
                    poster_url = db[key]
        except Exception:
            pass

        try:
            poster_tmp = await get_series_poster_async(series_name)
            poster_url = poster_tmp or poster_url
        except Exception:
            pass

        try:
            summary = await get_series_summary(series_name)
        except Exception as e:
            logger.warning(f"[SUGG_SUMMARY_FAIL] {series_name} -> {e}")
            summary = {"languages": [], "seasons": [], "qualities": []}

        seasons = summary.get("seasons", [])
        languages_list = ", ".join(summary.get("languages", []))
        qualities_list = ", ".join(summary.get("qualities", []))
        seasons_count = len(seasons)

        response = (
            f"**○ Title:** {series_name}\n"
            f"**○ Language:** {languages_list or 'N/A'}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in seasons:
            callback_data = f"gss|{series_name_short}|{season}"
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            # Edit the suggestion message to poster+details (edit_media if it was a photo is more complex;
            # we'll use edit_text to keep it simple and resilient)
            await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Failed to edit message for sugg_select: {e}")
            try:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass

        # schedule auto delete if configured
        try:
            auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
            if auto_delete_timer and int(auto_delete_timer) > 0:
                asyncio.create_task(delete_message_later(cyber, query.message.chat.id, query.message.id, int(auto_delete_timer)))
        except Exception:
            pass

    # -----------------------------------------------------
    # Close suggestion handler
    # -----------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^close_suggestion\|"))
    async def close_suggestion_handler(cyber, query: CallbackQuery):
        try:
            await cyber.delete_messages(query.message.chat.id, query.message.id)
        except Exception as e:
            logger.warning(f"Failed to delete suggestion message: {e}")
            await query.answer("❌ Failed to close message.", show_alert=True)
            return
        await query.answer("Closed.", show_alert=False)

    # -----------------------------------------------------
    # The group selection callbacks: gss| -> gls| -> gqs| (same as earlier)
    # -----------------------------------------------------
    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def gss_handler(cyber, query: CallbackQuery):
        data = query.data.split("|")
        if len(data) < 3:
            await query.answer("❌ Error: Invalid season selection data.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number.", show_alert=True)
            return

        try:
            languages = await get_unique_languages_by_season(series_name, season)
        except Exception:
            # fallback to series summary
            try:
                summary = await get_series_summary(series_name)
                languages = summary.get("languages", [])
            except Exception:
                languages = []

        if not languages:
            await query.answer(f"❌ No languages found for {series_name} S{season}.", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for lang in languages:
            callback_data = f"gls|{series_name_short}|{season}|{lang}"
            keyboard.append([InlineKeyboardButton(f"🔸 {lang} 🔸", callback_data=callback_data)])

        keyboard.append([InlineKeyboardButton("↺ Back to Search", callback_data=f"gback|{series_name_short}")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        caption = (
            f"📺 Title **{series_name}** — Season **{season}**\n\n"
            f"Please select your preferred audio language 👇"
        )

        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message in gss_handler: {e}")

        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def gls_handler(cyber, query: CallbackQuery):
        data = query.data.split("|")
        if len(data) < 4:
            await query.answer("❌ Error: Invalid language selection data.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number.", show_alert=True)
            return
        language = data[3]

        try:
            qualities = await get_unique_qualities_by_lang_season(series_name, language, season)
        except Exception:
            try:
                summary = await get_series_summary(series_name)
                qualities = summary.get("qualities", [])
            except Exception:
                qualities = []

        if not qualities:
            await query.answer(f"❌ No quality files found for S{season} ({language}).", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for quality in qualities:
            callback_data = f"gqs|{series_name_short}|{season}|{language}|{quality}"
            keyboard.append([InlineKeyboardButton(f"🔹 {quality} 🔹", callback_data=callback_data)])

        keyboard.append([InlineKeyboardButton("↺ Back to Languages", callback_data=f"gss|{series_name_short}|{season}")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        caption = (
            f"🎬 {series_name} – Season {season} ({language})\n\n"
            f"📌 Quality Selector\n\n"
            f"Tap a quality below — Bot will provide a PM link to receive files."
        )

        try:
            await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message in gls_handler: {e}")

        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def gqs_handler(cyber, query: CallbackQuery):
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

        # Create deep link
        bot = await cyber.get_me()
        start_param = _encode_start_payload(series_name, season, language, quality)
        deep_link = f"https://t.me/{bot.username}?start={start_param}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Open Bot (PM)", url=deep_link)]
        ])

        caption = (
            f"✅ Ready — {series_name} S{season} ({language}) [{quality}]\n\n"
            f"Tap **Open Bot (PM)** to start receiving files."
        )

        try:
            await query.message.edit_text(caption, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Failed to edit message for gqs: {e}")

        await query.answer("Open the bot in private chat to receive files.", show_alert=True)

    @cyber.on_callback_query(filters.regex(r"^gback\|"))
    async def gback_handler(_, query: CallbackQuery):
        data = query.data.split("|")
        if len(data) < 2:
            await query.answer()
            return

        series_name = data[1]
        try:
            summary = await get_series_summary(series_name)
            seasons = summary.get("seasons", [])
        except Exception:
            seasons = []

        if not seasons:
            await query.answer("No seasons found", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in seasons:
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=f"gss|{series_name_short}|{season}")])

        caption = f"**{series_name}**\n\nSelect Season:"
        try:
            await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=enums.ParseMode.MARKDOWN)
        except Exception:
            pass

        await query.answer()

    # end register_groupfilter_handlers
