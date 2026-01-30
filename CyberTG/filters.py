from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import DEFAULT_POSTER
from DB.database import (
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from helpers.series.helper import resolve_series
from helpers.filter.helper import encode_payload, decode_payload
from .poster import get_series_poster_async
from .files import set_session, deliver_files_direct

def register_filters(cyber: Client):

    @cyber.on_message(filters.command("start") & filters.private)
    async def start(_, m):

        if len(m.command) < 2:
            return

        uid, series, season, lang, quality = decode_payload(m.command[1])

        set_session(m.from_user.id, {
            "series": series,
            "season": int(season),
            "language": lang,
            "quality": quality
        })

        await deliver_files_direct(cyber, m.from_user.id)


    @cyber.on_message(filters.text & filters.private)
    async def pm_search(_, m):

        series = await resolve_series(m.text)
        if not series:
            return await m.reply("No result")

        summary = await get_series_summary(series)

        kb = [[InlineKeyboardButton(f"Season {s}", callback_data=f"ss|{series}|{s}")]
              for s in summary["seasons"]]

        await m.reply(series, reply_markup=InlineKeyboardMarkup(kb))


    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m):

        if not m.text or m.text.startswith("/"):
            return

        series = await resolve_series(m.text)
        if not series:
            return

        poster = await get_series_poster_async(series) or DEFAULT_POSTER
        summary = await get_series_summary(series)
        uid = m.from_user.id

        kb = [[InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{uid}|{series}|{s}")]
              for s in summary["seasons"]]

        await m.reply_photo(
            poster,
            caption=f"{series}\n\nSelect Season",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )


    @cyber.on_callback_query(filters.regex("^ss\\|"))
    async def pm_season(_, q):
        _, series, season = q.data.split("|")
        langs = await get_unique_languages_by_season(series, int(season))

        kb = [[InlineKeyboardButton(l, callback_data=f"ls|{series}|{season}|{l}")] for l in langs]
        await q.message.edit_text("Select Language", reply_markup=InlineKeyboardMarkup(kb))


    @cyber.on_callback_query(filters.regex("^ls\\|"))
    async def pm_lang(_, q):
        _, series, season, lang = q.data.split("|")
        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))

        kb = [[InlineKeyboardButton(x, callback_data=f"qs|{series}|{season}|{lang}|{x}")] for x in qs]
        await q.message.edit_text("Select Quality", reply_markup=InlineKeyboardMarkup(kb))


    @cyber.on_callback_query(filters.regex("^qs\\|"))
    async def pm_quality(_, q):
        _, series, season, lang, quality = q.data.split("|")

        set_session(q.from_user.id, {
            "series": series,
            "season": int(season),
            "language": lang,
            "quality": quality
        })

        await deliver_files_direct(cyber, q.from_user.id)


    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):
        _, uid, series, season, lang, quality = q.data.split("|")

        payload = encode_payload(series, season, lang, quality, uid)
        bot = await cyber.get_me()

        link = f"https://t.me/{bot.username}?start={payload}"

        await q.message.reply(
            "Send in PM",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶ Send Files", url=link)]
            ])
        )
