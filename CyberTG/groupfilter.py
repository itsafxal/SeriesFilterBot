# groupfilter.py
import asyncio
import base64
from urllib.parse import quote, unquote
import shelve

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
    OMDB_CACHE_DB
)

from utils import clean_series_name
from utils import check_force_subscribe

from DB.database import (
    search_series,
    get_series_summary,
)

from .poster import get_series_poster_async


# --------------------------------------------------------------------
# POSTER BUILDER
# --------------------------------------------------------------------
async def _build_poster_for_series(series_name: str):
    """
    Return cached poster or fetch using get_series_poster_async().
    Returns DEFAULT_POSTER if not available.
    """
    poster_url = DEFAULT_POSTER

    # 1) Try cache first
    try:
        cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
        with shelve.open(cache_file) as cache:
            key = f"poster::{series_name.strip().lower()}"
            if key in cache:
                poster_url = cache[key]
    except Exception:
        pass

    # 2) Fetch fresh from OMDB if default or empty
    try:
        poster_tmp = await get_series_poster_async(series_name)
        poster_url = poster_tmp or DEFAULT_POSTER
    except Exception as e:
        logger.warning(f"[POSTER FETCH FAIL] {series_name} -> {e}")
        poster_url = DEFAULT_POSTER

    return poster_url


# --------------------------------------------------------------------
# DEEP LINK ENCODER
# --------------------------------------------------------------------
def _encode_start_payload(series: str, season: int, language: str, quality: str) -> str:
    raw = f"{series}|{season}|{language}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"send_{quote(b64)}"


# --------------------------------------------------------------------
# HANDLER REGISTRATION
# --------------------------------------------------------------------
def register_groupfilter_handlers(cyber: Client):

    # ============================================================
    # 1. GROUP TEXT SEARCH (OPTION A)
    # ============================================================
    @cyber.on_message(filters.text & filters.group)
    async def group_search_handler(_, message: Message):

        text = (message.text or "").strip()
        if not text:
            return

        # Ignore commands, links, mentions
        if (
            text.startswith("/")
            or text.startswith(".")
            or text.startswith("#")
            or "http://" in text
            or "https://" in text
        ):
            return

        # Search
        try:
            results = await search_series(text)
        except Exception as e:
            logger.error(f"[GROUP SEARCH ERR] {e}")
            results = []

        if not results:
            return  # do not spam groups

        series = clean_series_name(results[0])

        # Poster
        poster = await _build_poster_for_series(series)

        # Summary: seasons, languages, qualities
        try:
            summary = await get_series_summary(series)
            seasons = summary.get("seasons", [])
        except Exception as e:
            logger.error(f"[SUMMARY ERR] {e}")
            seasons = []

        if not seasons:
            return

        # Build Season keyboard (gss|)
        keyboard = []
        series_short = series[:MAX_CALLBACK_LENGTH]

        for s in seasons:
            keyboard.append([
                InlineKeyboardButton(
                    f"Season {s}",
                    callback_data=f"gss|{series_short}|{s}"
                )
            ])

        # Optionally PM redirect
        keyboard.append([InlineKeyboardButton("PM Mode", switch_inline_query_current_chat="")])

        caption = (
            f"**{series}**\n\n"
            f"Available Seasons: {len(seasons)}\n\n"
            f"Select a season to continue:"
        )

        try:
            await message.reply_photo(
                poster,
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception:
            await message.reply_text(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )

    # ============================================================
    # 2. SEASON SELECTED (gss|)
    # ============================================================
    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def gss_handler(cyber, query: CallbackQuery):

        try:
            _, series, season = query.data.split("|")
            season = int(season)
        except:
            await query.answer("Invalid data", show_alert=True)
            return

        # Summary for languages
        try:
            summary = await get_series_summary(series)
            languages = summary.get("languages", [])
        except:
            languages = []

        if not languages:
            await query.answer("No languages found", show_alert=True)
            return

        keyboard = []
        series_short = series[:MAX_CALLBACK_LENGTH]

        for lang in languages:
            keyboard.append([
                InlineKeyboardButton(
                    lang,
                    callback_data=f"gls|{series_short}|{season}|{lang}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton("↺ Back", callback_data=f"gback|{series_short}")
        ])

        caption = (
            f"🎬 **{series}** — Season {season}\n\n"
            f"Select Audio Language:"
        )

        try:
            await query.message.edit_caption(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            try:
                await query.message.edit_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except:
                pass

        await query.answer()

    # ============================================================
    # 3. LANGUAGE SELECTED (gls|)
    # ============================================================
    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def gls_handler(_, query: CallbackQuery):

        try:
            _, series, season, language = query.data.split("|")
            season = int(season)
        except:
            await query.answer("Invalid data", show_alert=True)
            return

        # Summary for qualities
        try:
            summary = await get_series_summary(series)
            qualities = summary.get("qualities", [])
        except:
            qualities = []

        if not qualities:
            await query.answer("No qualities found", show_alert=True)
            return

        series_short = series[:MAX_CALLBACK_LENGTH]
        keyboard = []

        for q in qualities:
            keyboard.append([
                InlineKeyboardButton(
                    q,
                    callback_data=f"gqs|{series_short}|{season}|{language}|{q}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                "↺ Back",
                callback_data=f"gss|{series_short}|{season}"
            )
        ])

        caption = (
            f"📺 **{series}** — S{season} ({language})\n\n"
            f"Select Quality:"
        )

        try:
            await query.message.edit_caption(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            try:
                await query.message.edit_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except:
                pass

        await query.answer()

    # ============================================================
    # 4. QUALITY SELECTED → SEND DEEP LINK (gqs|)
    # ============================================================
    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def gqs_handler(cyber, query: CallbackQuery):

        try:
            _, series, season, language, quality = query.data.split("|")
            season = int(season)
        except:
            await query.answer("Invalid data", show_alert=True)
            return

        # Encode deep-link
        param = _encode_start_payload(series, season, language, quality)
        bot = await cyber.get_me()
        deeplink = f"https://t.me/{bot.username}?start={param}"

        keyboard = [
            [InlineKeyboardButton("🔐 Open Bot", url=deeplink)],
            [InlineKeyboardButton("Check Subscribe", callback_data=f"gcheck|{series}|{season}|{language}|{quality}")]
        ]

        caption = (
            f"✔ **{series} — S{season} ({language}) [{quality}]**\n\n"
            f"Tap *Open Bot* to receive files in PM."
        )

        try:
            await query.message.edit_caption(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            try:
                await query.message.edit_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except:
                pass

        await query.answer("Open bot in PM to continue", show_alert=True)

    # ============================================================
    # 5. FORCE-SUB CHECK BUTTON (gcheck|)
    # ============================================================
    @cyber.on_callback_query(filters.regex(r"^gcheck\|"))
    async def gcheck_handler(cyber, query: CallbackQuery):

        try:
            _, series, season, language, quality = query.data.split("|")
            season = int(season)
        except:
            await query.answer("Invalid data", show_alert=True)
            return

        is_member, link = await check_force_subscribe(cyber, query.from_user.id)

        if not is_member:
            btn = [[InlineKeyboardButton("📣 Join Channel", url=link)]]
            await query.answer("Join channel first", show_alert=True)
            await query.message.reply_text(
                "You must join the required channel to continue.",
                reply_markup=InlineKeyboardMarkup(btn),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            return

        # If subscribed → send PM link
        param = _encode_start_payload(series, season, language, quality)
        bot = await cyber.get_me()
        deeplink = f"https://t.me/{bot.username}?start={param}"

        await query.answer("You are subscribed!", show_alert=True)
        await query.message.reply_text(
            f"✔ You are subscribed.\nOpen bot:\n{deeplink}"
        )

    # ============================================================
    # 6. BACK BUTTON (gback|)
    # ============================================================
    @cyber.on_callback_query(filters.regex(r"^gback\|"))
    async def gback_handler(_, query: CallbackQuery):

        try:
            _, series = query.data.split("|")
        except:
            await query.answer()
            return

        # load seasons again
        try:
            summary = await get_series_summary(series)
            seasons = summary.get("seasons", [])
        except:
            seasons = []

        if not seasons:
            await query.answer("Not found", show_alert=True)
            return

        keyboard = []
        series_short = series[:MAX_CALLBACK_LENGTH]

        for s in seasons:
            keyboard.append([
                InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{series_short}|{s}")
            ])

        keyboard.append([InlineKeyboardButton("PM Mode", switch_inline_query_current_chat="")])

        caption = f"**{series}**\n\nSelect a season:"

        try:
            await query.message.edit_caption(
                caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except:
            try:
                await query.message.edit_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except:
                pass

        await query.answer()

    # end of register function

