import asyncio
import base64
import shelve
from urllib.parse import quote

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

from config import DEFAULT_POSTER, OMDB_CACHE_DB
from utils import clean_series_name, delete_message_later
from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)
from .poster import get_series_poster_async


def _encode_payload(series, season, lang, quality, uid):
    raw = f"{uid}|{series}|{season}|{lang}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"send_{b64}"



async def _poster(series):
    p = DEFAULT_POSTER
    try:
        with shelve.open(OMDB_CACHE_DB or "/tmp/omdb_cache.db") as db:
            k = f"poster::{series.lower()}"
            if k in db:
                p = db[k]
    except:
        pass

    try:
        np = await get_series_poster_async(series)
        if np:
            p = np
    except:
        pass
    return p


def register_groupfilter_handlers(cyber: Client):

    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m: Message):

        if not m.text or m.text.startswith("/"):
            return

        res = await search_series(m.text)
        if not res:
            return

        series = clean_series_name(res[0])
        await _send_series(cyber, m, series)

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        langs = await get_unique_languages_by_season(series, int(season))
        kb = [[InlineKeyboardButton(l, callback_data=f"gls|{uid}|{series}|{season}|{l}")] for l in langs]
        await q.message.edit_caption("Select Language", reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))
        kb = [[InlineKeyboardButton(x, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{x}")] for x in qs]
        await q.message.edit_caption("Select Quality", reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()

    # FINAL STEP → MANUAL BUTTON
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
        await q.answer()

async def _send_series(cyber, m, series):

    poster = await _poster(series)
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
