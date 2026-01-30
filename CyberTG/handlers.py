import asyncio
import shelve
import base64

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from config import (
    logger,
    DEFAULT_POSTER,
    OMDB_CACHE_DB,
)

from utils import clean_series_name, check_force_subscribe
from .poster import get_series_poster_async
from .session_store import set_session
from .delivery import deliver_files_direct


# ------------------------------------------------
# /start deep-link receiver
# ------------------------------------------------
@Client.on_message(filters.command("start") & filters.private)
async def deep_link_start(cyber, message: Message):

    if len(message.command) < 2:
        return

    payload = message.command[1]

    if not payload.startswith("send_"):
        return

    try:
        raw = base64.urlsafe_b64decode(payload.replace("send_", "")).decode()
        owner, series, season, lang, quality = raw.split("|")
        season = int(season)
    except Exception:
        return await message.reply("❌ Invalid link")

    set_session(message.from_user.id, {
        "series": series,
        "season": season,
        "language": lang,
        "quality": quality
    })

    await message.reply(
        f"📥 Sending files\n\n"
        f"{series}\nSeason {season}\n{lang} | {quality}"
    )

    await deliver_files_direct(cyber, message.from_user.id)


# ------------------------------------------------
# Register private handlers
# ------------------------------------------------
def register_conversation_handlers(cyber: Client):

    # ---------------- SEARCH ----------------
    @cyber.on_message(filters.text & filters.private & ~filters.command(["start"]))
    async def search_handler(cyber, message: Message):

        query = message.text.strip()

        is_member, invite = await check_force_subscribe(cyber, message.from_user.id)
        if not is_member:
            return await message.reply(
                "Join channel first 👇",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Join Channel", url=invite)]
                ])
            )

        try:
            results = await search_series(query)
        except Exception as e:
            logger.error(e)
            results = []

        if not results:
            return await message.reply("❌ No results found")

        series = clean_series_name(results[0])

        poster = DEFAULT_POSTER

        try:
            with shelve.open(OMDB_CACHE_DB or "/tmp/omdb_cache.db") as db:
                key = f"poster::{series.lower()}"
                if key in db:
                    poster = db[key]
        except:
            pass

        try:
            p = await get_series_poster_async(series)
            if p:
                poster = p
        except:
            pass

        summary = await get_series_summary(series)

        caption = (
            f"**{series}**\n\n"
            f"Languages: {', '.join(summary.get('languages', []))}\n"
            f"Seasons: {len(summary.get('seasons', []))}\n"
            f"Quality: {', '.join(summary.get('qualities', []))}\n\n"
            f"Select Season 👇"
        )

        kb = [
            [InlineKeyboardButton(f"Season {s}", callback_data=f"ss|{series}|{s}")]
            for s in summary.get("seasons", [])
        ]

        await message.reply_photo(
            poster,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )


    # ---------------- SEASON ----------------
    @cyber.on_callback_query(filters.regex("^ss\\|"))
    async def season(_, q: CallbackQuery):

        _, series, season = q.data.split("|")
        season = int(season)

        langs = await get_unique_languages_by_season(series, season)

        kb = [
            [InlineKeyboardButton(l, callback_data=f"ls|{series}|{season}|{l}")]
            for l in langs
        ]

        await q.message.edit_caption("Select Language 👇", reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()


    # ---------------- LANGUAGE ----------------
    @cyber.on_callback_query(filters.regex("^ls\\|"))
    async def language(_, q: CallbackQuery):

        _, series, season, lang = q.data.split("|")

        qualities = await get_unique_qualities_by_lang_season(series, lang, int(season))

        kb = [
            [InlineKeyboardButton(qy, callback_data=f"qs|{series}|{season}|{lang}|{qy}")]
            for qy in qualities
        ]

        await q.message.edit_caption("Select Quality 👇", reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()


    # ---------------- QUALITY ----------------
    @cyber.on_callback_query(filters.regex("^qs\\|"))
    async def quality(_, q: CallbackQuery):

        _, series, season, lang, quality = q.data.split("|")

        set_session(q.from_user.id, {
            "series": series,
            "season": int(season),
            "language": lang,
            "quality": quality
        })

        await q.answer("📥 Sending…")
        await deliver_files_direct(cyber, q.from_user.id)


    return [search_handler]
