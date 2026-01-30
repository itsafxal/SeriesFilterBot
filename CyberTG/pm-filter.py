from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from utils import (
    clean_series_name,
    clean_query,
    fuzzy_match,
    strict_match_only,
    spell_correct,
)

from .session_store import set_session
from .delivery import deliver_files_direct


def register_conversation_handlers(cyber: Client):

    @cyber.on_message(filters.text & filters.private)
    async def search(_, m):

        query = clean_query(m.text)
        query = spell_correct(query)

        rows = await search_series(query)
        if not rows:
            return await m.reply("No result")

        titles = [clean_series_name(x) for x in rows]

        match = fuzzy_match(query, titles)
        if not match:
            match = strict_match_only(query, titles)

        if not match:
            return await m.reply("No close match")

        series = match

        summary = await get_series_summary(series)

        kb = [[InlineKeyboardButton(f"Season {s}", callback_data=f"ss|{series}|{s}")]
              for s in summary["seasons"]]

        await m.reply(series, reply_markup=InlineKeyboardMarkup(kb))

    @cyber.on_callback_query(filters.regex("^ss\\|"))
    async def ss(_, q):
        _, series, season = q.data.split("|")

        langs = await get_unique_languages_by_season(series, int(season))
        kb = [[InlineKeyboardButton(l, callback_data=f"ls|{series}|{season}|{l}")] for l in langs]

        await q.message.edit_text("Select Language", reply_markup=InlineKeyboardMarkup(kb))

    @cyber.on_callback_query(filters.regex("^ls\\|"))
    async def ls(_, q):
        _, series, season, lang = q.data.split("|")

        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))
        kb = [[InlineKeyboardButton(x, callback_data=f"qs|{series}|{season}|{lang}|{x}")] for x in qs]

        await q.message.edit_text("Select Quality", reply_markup=InlineKeyboardMarkup(kb))

    @cyber.on_callback_query(filters.regex("^qs\\|"))
    async def qs(_, q):
        _, series, season, lang, quality = q.data.split("|")

        set_session(q.from_user.id, {
            "series": series,
            "season": int(season),
            "language": lang,
            "quality": quality
        })

        await q.answer("Sending…")
        await deliver_files_direct(cyber, q.from_user.id)
