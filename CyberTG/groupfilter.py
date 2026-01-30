import asyncio
import base64
import re
import shelve
from urllib.parse import quote

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    InputMediaPhoto,
)

from config import logger, DEFAULT_POSTER, OMDB_CACHE_DB, SPELL_IMG
from utils import clean_series_name, delete_message_later

from DB.database import (
    get_global_setting,
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from .poster import get_series_poster_async


# ------------------------------------------------
# DEEPLINK ENCODE
# ------------------------------------------------
def _encode_payload(series, season, lang, quality, uid):
    raw = f"{uid}|{series}|{season}|{lang}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"send_{quote(b64)}"


# ------------------------------------------------
async def _build_poster(series):
    poster = DEFAULT_POSTER
    try:
        with shelve.open(OMDB_CACHE_DB or "/tmp/omdb_cache.db") as db:
            k = f"poster::{series.lower()}"
            if k in db:
                poster = db[k]
    except:
        pass

    try:
        p = await get_series_poster_async(series)
        if p:
            poster = p
    except:
        pass

    return poster


# ------------------------------------------------
def register_groupfilter_handlers(cyber: Client):

    # ---------------- SEARCH ----------------
    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m: Message):

        text = (m.text or "").strip()
        if not text or text.startswith("/") or "http" in text:
            return

        try:
            res = await search_series(text)
        except:
            res = []

        res = [clean_series_name(r) for r in res]

        if not res:
            return

        await _send_series(cyber, m, res[0])


    # ---------------- SEASON ----------------
    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):

        _, uid, series, season = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        season = int(season)
        langs = await get_unique_languages_by_season(series, season)

        kb = [[InlineKeyboardButton(l, callback_data=f"gls|{uid}|{series}|{season}|{l}")]
              for l in langs]

        await q.message.edit_caption("🌐 Select Language", reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()


    # ---------------- LANGUAGE ----------------
    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):

        _, uid, series, season, lang = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        qualities = await get_unique_qualities_by_lang_season(series, lang, int(season))

        kb = [[InlineKeyboardButton(qy, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{qy}")]
              for qy in qualities]

        await q.message.edit_caption("🎞 Select Quality", reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()


    # ---------------- QUALITY (MANUAL BUTTON FIX) ----------------
    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):

        _, uid, series, season, lang, quality = q.data.split("|")

        if q.from_user.id != int(uid):
            return await q.answer("Not for you", show_alert=True)

        payload = _encode_payload(series, season, lang, quality, uid)
        bot = await cyber.get_me()

        link = f"https://t.me/{bot.username}?start={payload}"

        # MANUAL BUTTON (Telegram bug workaround)
        await q.message.reply(
            "📥 Tap button to receive files in PM",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Send Files", url=link)]
            ])
        )

        await q.answer()


# ------------------------------------------------
async def _send_series(cyber, m, series):

    poster = await _build_poster(series)
    summary = await get_series_summary(series)

    caption = (
        f"**{series}**\n\n"
        f"Languages: {', '.join(summary.get('languages', []))}\n"
        f"Seasons: {len(summary.get('seasons', []))}\n"
        f"Quality: {', '.join(summary.get('qualities', []))}\n\n"
        f"Select Season 👇"
    )

    uid = m.from_user.id

    kb = [[InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{uid}|{series}|{s}")]
          for s in summary.get("seasons", [])]

    sent = await m.reply_photo(
        poster,
        caption=caption,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=enums.ParseMode.MARKDOWN
    )

    t = await get_global_setting("AUTO_DELETE_TIMER", 0)
    if t:
        asyncio.create_task(delete_message_later(cyber, sent.chat.id, sent.id, int(t)))
