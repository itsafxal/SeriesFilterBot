import math
import asyncio

from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    CallbackQuery
)

from config import DEFAULT_POSTER, SPELL_IMG
from DB.database import (
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
    search_series
)

from utils.series_helper import suggest_series
from utils.telegram_helper import delete_message_later
from utils.filter_helper import encode_payload, decode_payload

from files import set_session, deliver_files_direct
from .poster import get_series_info


SEASONS_PER_PAGE = 9


# -------------------------------------------------
# STRICT MATCH
# -------------------------------------------------

def strict_match(query: str, names: list):
    q = query.lower().strip()
    return [n for n in names if n.lower().strip() == q]


# -------------------------------------------------
# CAPTION BUILDER
# -------------------------------------------------

def build_caption(series, year, genre, rating,
                  seasons=None, languages=None,
                  qualities=None, step="season"):

    lines = [
        f"○ Title: {series}",
        f"○ Released On: {year or 'N/A'}",
        f"○ Genre: {genre or 'N/A'}",
        f"○ Rating: {rating or 'N/A'}",
        ""
    ]

    if step == "season":
        if seasons:
            lines.append("○ Available Seasons: " +
                         ", ".join(f"S{s}" for s in seasons))
        lines.append("")
        lines.append("Select an available season...!")

    elif step == "language":
        lines.append(f"○ Available Languages: {', '.join(languages)}")
        lines.append("")
        lines.append("Select an available language...!")

    elif step == "quality":
        lines.append(f"○ Available Languages: {', '.join(languages)}")
        lines.append(f"○ Available Quality: {', '.join(qualities)}")
        lines.append("")
        lines.append("Select an available quality...!")

    return "\n".join(lines)


# -------------------------------------------------
# KEYBOARD HELPERS
# -------------------------------------------------

def build_grid_keyboard(items, cb_builder, per_row=2):
    rows, row = [], []
    for it in items:
        row.append(InlineKeyboardButton(str(it), callback_data=cb_builder(it)))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_season_pages(seasons, uid, series, page=0):

    total_pages = math.ceil(len(seasons) / SEASONS_PER_PAGE)

    start = page * SEASONS_PER_PAGE
    end = start + SEASONS_PER_PAGE
    chunk = seasons[start:end]

    rows, row = [], []

    for s in chunk:
        row.append(
            InlineKeyboardButton(
                f"Season {s}",
                callback_data=f"gss|{uid}|{series}|{s}"
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀ Prev",
                                     callback_data=f"spg|{uid}|{series}|{page-1}")
            )

        nav.append(
            InlineKeyboardButton(
                f"{page+1}/{total_pages}",
                callback_data="noop"
            )
        )

        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("Next ▶",
                                     callback_data=f"spg|{uid}|{series}|{page+1}")
            )

        rows.append(nav)

    return InlineKeyboardMarkup(rows)


# -------------------------------------------------
# MAIN REGISTER
# -------------------------------------------------

def register_filters(cyber: Client):

    async def show_suggestions(message, suggestions):

        try:
            await message.delete()
        except:
            pass

        kb = [
            [InlineKeyboardButton(x, callback_data=f"sug|{x}")]
            for x in suggestions
        ]

        kb.append([InlineKeyboardButton("Close", callback_data="close_spell")])

        sent = await cyber.send_photo(
            message.chat.id,
            SPELL_IMG,
            caption="❓ <b>Spell Check</b>\n\nDid you mean one of these?",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb)
        )

        await delete_message_later(cyber, sent.chat.id, sent.id, 40)

    # -------------------------------------------------
    # PRIVATE SEARCH
    # -------------------------------------------------

    @cyber.on_message(filters.text & filters.private)
    async def pm_search(_, m):

        text = (m.text or "").strip()
        if not text:
            return

        results = await search_series(text)
        strict = strict_match(text, results)

        if strict:
            series = strict[0]
        else:
            sugg = await suggest_series(text)
            if sugg:
                return await show_suggestions(m, sugg)
            return await m.reply("❌ No result")

        poster, year, genre, rating = await get_series_info(series)
        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])

        caption = build_caption(series, year, genre, rating,
                                seasons=seasons, step="season")

        kb = build_season_pages(seasons, m.from_user.id, series)

        await m.reply_photo(poster or DEFAULT_POSTER,
                            caption=caption,
                            reply_markup=kb)

    # -------------------------------------------------
    # GROUP SEARCH
    # -------------------------------------------------

    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m):

        if not m.text or m.text.startswith("/"):
            return

        results = await search_series(m.text)
        strict = strict_match(m.text, results)

        if strict:
            series = strict[0]
        else:
            sugg = await suggest_series(m.text)
            if sugg:
                return await show_suggestions(m, sugg)
            return

        poster, year, genre, rating = await get_series_info(series)
        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])

        caption = build_caption(series, year, genre, rating,
                                seasons=seasons, step="season")

        kb = build_season_pages(seasons, m.from_user.id, series)

        await m.reply_photo(poster or DEFAULT_POSTER,
                            caption=caption,
                            reply_markup=kb)

    # -------------------------------------------------
    # SPELL SELECT
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^sug\\|"))
    async def sug(_, q):

        series = q.data.split("|", 1)[1]
        poster, year, genre, rating = await get_series_info(series)
        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])

        caption = build_caption(series, year, genre, rating,
                                seasons=seasons, step="season")

        kb = build_season_pages(seasons, q.from_user.id, series)

        await q.message.edit_media(
            InputMediaPhoto(poster or DEFAULT_POSTER, caption=caption),
            reply_markup=kb
        )

        await q.answer()

    @cyber.on_callback_query(filters.regex("^close_spell$"))
    async def close_spell(_, q):
        await q.message.delete()
        await q.answer()

    # -------------------------------------------------
    # SEASON → LANGUAGE
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):

        _, uid, series, season = q.data.split("|")

        langs = await get_unique_languages_by_season(series, int(season))
        poster, year, genre, rating = await get_series_info(series)

        caption = build_caption(series, year, genre, rating,
                                languages=langs, step="language")

        kb = build_grid_keyboard(
            langs,
            lambda l: f"gls|{uid}|{series}|{season}|{l}"
        )

        await q.message.edit_caption(caption, reply_markup=kb)
        await q.answer()

    # -------------------------------------------------
    # LANGUAGE → QUALITY
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):

        _, uid, series, season, lang = q.data.split("|")

        qs = await get_unique_qualities_by_lang_season(
            series, lang, int(season)
        )

        poster, year, genre, rating = await get_series_info(series)

        caption = build_caption(series, year, genre, rating,
                                languages=[lang],
                                qualities=qs,
                                step="quality")

        kb = build_grid_keyboard(
            qs,
            lambda x: f"gqs|{uid}|{series}|{season}|{lang}|{x}"
        )

        await q.message.edit_caption(caption, reply_markup=kb)
        await q.answer()

    # -------------------------------------------------
    # QUALITY HANDLER
    # -------------------------------------------------

    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):

        _, uid, series, season, lang, quality = q.data.split("|")

        payload = encode_payload(series, season, lang, quality, uid)

        bot = await cyber.get_me()
        link = f"https://t.me/{bot.username}?start={payload}"

        # PRIVATE → DIRECT SEND (NO /start MESSAGE)
        if q.message.chat.type in ["private", enums.ChatType.PRIVATE]:

            decoded = decode_payload(payload)
            uid, series, season, lang, quality = decoded

            set_session(int(uid), {
                "series": series,
                "season": int(season),
                "language": lang,
                "quality": quality
            })

            await deliver_files_direct(cyber, int(uid))

            try:
                await q.message.edit_caption("📥 Sending files...")
            except:
                await q.message.edit_text("📥 Sending files...")

            return await q.answer("Sending files...")

        # GROUP → BUTTON
        await q.message.edit_caption(
            "📥 Redirecting to PM…",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("☘️ DOWNLOAD FILES ☘️", url=link)]
            ])
        )

        await q.answer()
