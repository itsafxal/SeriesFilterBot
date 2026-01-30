import asyncio
import base64

from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import DEFAULT_POSTER
from utils import (
    clean_series_name,
    clean_query,
    fuzzy_match,
    strict_match_only,
    spell_correct,
)

from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from .poster import get_series_poster_async


def _encode_payload(series, season, lang, quality, uid):
    raw = f"{uid}|{series}|{season}|{lang}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"send_{b64}"


def register_groupfilter_handlers(cyber: Client):

    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m: Message):

        if not m.text or m.text.startswith("/"):
            return

        query = clean_query(m.text)
        query = spell_correct(query)

        rows = await search_series(query)
        if not rows:
            return

        titles = [clean_series_name(x) for x in rows]

        match = fuzzy_match(query, titles)
        if not match:
            match = strict_match_only(query, titles)

        if not match:
            return

        series = match

        poster = DEFAULT_POSTER
        try:
            p = await get_series_poster_async(series)
            if p:
                poster = p
        except:
            pass

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

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        langs = await get_unique_languages_by_season(series, int(season))
        kb = [[InlineKeyboardButton(l, callback_data=f"gls|{uid}|{series}|{season}|{l}")] for l in langs]

        try:
            await q.message.edit_caption("Select Language", reply_markup=InlineKeyboardMarkup(kb))
        except:
            pass

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))
        kb = [[InlineKeyboardButton(x, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{x}")] for x in qs]

        try:
            await q.message.edit_caption("Select Quality", reply_markup=InlineKeyboardMarkup(kb))
        except:
            pass

    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):
        _, uid, series, season, lang, quality = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        payload = _encode_payload(series, season, lang, quality, uid)
        bot = await cyber.get_me()
        link = f"https://t.me/{bot.username}?start={payload}"

        await q.message.reply(
            "📥 Tap to receive files in PM",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Send Files", url=link)]
            ])
        )
