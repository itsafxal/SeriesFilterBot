from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import DEFAULT_POSTER
from DB.database import (
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from utils.series_helper import resolve_series
from utils.filter_helper import encode_payload
from .poster import get_series_poster_async
from .files import set_session, deliver_files_direct


def register_filters(cyber: Client):

    # ==============================
    # PRIVATE SEARCH
    # ==============================
    @cyber.on_message(filters.text & filters.private)
    async def pm_search(_, m):

        series = await resolve_series(m.text)
        if not series:
            return await m.reply("❌ No result")

        summary = await get_series_summary(series)

        kb = [
            [InlineKeyboardButton(f"Season {s}", callback_data=f"ss|{series}|{s}")]
            for s in summary["seasons"]
        ]

        await m.reply(series, reply_markup=InlineKeyboardMarkup(kb))


    # ==============================
    # GROUP SEARCH
    # ==============================
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

        kb = [
            [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{uid}|{series}|{s}")]
            for s in summary["seasons"]
        ]

        await m.reply_photo(
            poster,
            caption=f"{series}\n\nSelect Season",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )


    # ==============================
    # PM SEASON
    # ==============================
    @cyber.on_callback_query(filters.regex("^ss\\|"))
    async def pm_season(_, q):
        _, series, season = q.data.split("|")

        langs = await get_unique_languages_by_season(series, int(season))

        kb = [[InlineKeyboardButton(l, callback_data=f"ls|{series}|{season}|{l}")]
              for l in langs]

        await q.message.edit_text("Select Language", reply_markup=InlineKeyboardMarkup(kb))


    # ==============================
    # PM LANGUAGE
    # ==============================
    @cyber.on_callback_query(filters.regex("^ls\\|"))
    async def pm_lang(_, q):
        _, series, season, lang = q.data.split("|")

        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))

        kb = [[InlineKeyboardButton(x, callback_data=f"qs|{series}|{season}|{lang}|{x}")]
              for x in qs]

        await q.message.edit_text("Select Quality", reply_markup=InlineKeyboardMarkup(kb))


    # ==============================
    # PM QUALITY → SEND FILES
    # ==============================
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


    # ==============================
    # GROUP SEASON
    # ==============================
    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|")

        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        langs = await get_unique_languages_by_season(series, int(season))

        kb = [[InlineKeyboardButton(l, callback_data=f"gls|{uid}|{series}|{season}|{l}")]
              for l in langs]

        await q.message.edit_caption("Select Language", reply_markup=InlineKeyboardMarkup(kb))


    # ==============================
    # GROUP LANGUAGE
    # ==============================
    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|")

        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))

        kb = [[InlineKeyboardButton(x, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{x}")]
              for x in qs]

        await q.message.edit_caption("Select Quality", reply_markup=InlineKeyboardMarkup(kb))


    # ==============================
    # GROUP QUALITY → AUTO PM SEND
    # ==============================
    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):
        _, uid, series, season, lang, quality = q.data.split("|")

        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        # store session
        set_session(uid, {
            "series": series,
            "season": int(season),
            "language": lang,
            "quality": quality
        })

        try:
            # try direct PM delivery
            await deliver_files_direct(cyber, uid)
            await q.answer("📥 Sending files in PM…")

        except Exception:
            # fallback if user never started bot
            payload = encode_payload(series, season, lang, quality, uid)
            bot = await cyber.get_me()
            link = f"https://t.me/{bot.username}?start={payload}"

            await q.message.reply(
                "👉 Start bot in PM first:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶ Start Bot", url=link)]
                ])
            )
