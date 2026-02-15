import math

from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    InputMediaPhoto,
    CallbackQuery
)

from config import (
    DEFAULT_POSTER,
    SPELL_IMG,
    SEASONS_PER_PAGE
)

from DB.database import (
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
    search_series
)

from utils.series_helper import suggest_series, check_owner
from utils.telegram_helper import delete_message_later
from utils.filter_helper import encode_payload
from utils.font_helper import BTN, UI, small_caps

from .poster import get_series_info


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
        f"<b>○ {small_caps('Title')}: {small_caps(series)}</b>",
        f"<b>○ {small_caps('Released On')}: {small_caps(year or 'N/A')}</b>",
        f"<b>○ {small_caps('Genre')}: {small_caps(genre or 'N/A')}</b>",
        f"<b>○ {small_caps('Rating')}: {small_caps(rating or 'N/A')}</b>",
        ""
    ]

    if step == "season":
        if seasons:
            lines.append(
                f"<b>○ {small_caps('Available Seasons')}: "
                f"{', '.join(f'S{s}' for s in seasons)}</b>"
            )
        lines.append("")
        lines.append(f"<b>{small_caps('Select an available season')}...!</b>")

    elif step == "language":
        lang_txt = ", ".join(languages) if languages else "Unknown"
        lines.append(
            f"<b>○ {small_caps('Available Languages')}: {small_caps(lang_txt)}</b>"
        )
        lines.append("")
        lines.append(f"<b>{small_caps('Select an available language')}...!</b>")

    elif step == "quality":
        lang_txt = ", ".join(languages) if languages else "Unknown"
        qual_txt = ", ".join(qualities) if qualities else ""
        lines.append(
            f"<b>○ {small_caps('Available Languages')}: {small_caps(lang_txt)}</b>"
        )
        lines.append(
            f"<b>○ {small_caps('Available Quality')}: {small_caps(qual_txt)}</b>"
        )
        lines.append("")
        lines.append(f"<b>{small_caps('Select an available quality')}...!</b>")

    return "\n".join(lines)


# -------------------------------------------------
# KEYBOARD HELPERS
# -------------------------------------------------

def build_grid_keyboard(items, cb_builder, per_row=2):
    rows, row = [], []
    for it in items:
        row.append(BTN(str(it), callback_data=cb_builder(it)))
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
            BTN(
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
                BTN("◀ Prev",
                    callback_data=f"spg|{uid}|{series}|{page-1}")
            )

        nav.append(
            BTN(f"{page+1}/{total_pages}", callback_data="noop")
        )

        if page < total_pages - 1:
            nav.append(
                BTN("Next ▶",
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
            [BTN(x, callback_data=f"sug|{x}")]
            for x in suggestions
        ]

        kb.append([BTN("Close", callback_data="close_spell")])

        sent = await cyber.send_photo(
            message.chat.id,
            SPELL_IMG,
            caption=UI("Spell Check\n\nDid you mean one of these?"),
            reply_markup=InlineKeyboardMarkup(kb)
        )

        await delete_message_later(cyber, sent.chat.id, sent.id, 40)

    # ---------------- PRIVATE SEARCH ----------------

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

        caption = build_caption(
            series, year, genre, rating,
            seasons=seasons, step="season"
        )

        kb = build_season_pages(seasons, m.from_user.id, series)

        await m.reply_photo(
            poster or DEFAULT_POSTER,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb
        )

    # ---------------- GROUP SEARCH ----------------

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

        caption = build_caption(
            series, year, genre, rating,
            seasons=seasons, step="season"
        )

        kb = build_season_pages(seasons, m.from_user.id, series)

        await m.reply_photo(
            poster or DEFAULT_POSTER,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb
        )

    # ---------------- SPELL SELECT ----------------

    @cyber.on_callback_query(filters.regex("^sug\\|"))
    async def sug(_, q):

        series = q.data.split("|", 1)[1]

        poster, year, genre, rating = await get_series_info(series)
        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])

        caption = build_caption(
            series, year, genre, rating,
            seasons=seasons, step="season"
        )

        kb = build_season_pages(seasons, q.from_user.id, series)

        await q.message.edit_media(
            InputMediaPhoto(
                poster or DEFAULT_POSTER,
                caption=caption,
                parse_mode=enums.ParseMode.HTML
            ),
            reply_markup=kb
        )

        await q.answer()

    @cyber.on_callback_query(filters.regex("^close_spell$"))
    async def close_spell(_, q):
        await q.message.delete()
        await q.answer()

    # ---------------- PAGINATION ----------------

    @cyber.on_callback_query(filters.regex("^spg\\|"))
    async def season_page(_, q):

        _, uid, series, page = q.data.split("|")

        if not await check_owner(q, uid):
            return

        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])

        kb = build_season_pages(seasons, uid, series, int(page))

        await q.message.edit_reply_markup(kb)
        await q.answer()

    @cyber.on_callback_query(filters.regex("^noop$"))
    async def noop(_, q):
        await q.answer()

    # ---------------- SEASON → LANGUAGE ----------------

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):

        _, uid, series, season = q.data.split("|")

        if not await check_owner(q, uid):
            return

        langs = await get_unique_languages_by_season(series, int(season))
        poster, year, genre, rating = await get_series_info(series)

        caption = build_caption(
            series, year, genre, rating,
            languages=langs, step="language"
        )

        kb = build_grid_keyboard(
            langs,
            lambda l: f"gls|{uid}|{series}|{season}|{l}"
        )

        await q.message.edit_caption(
            caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb
        )

        await q.answer()

    # ---------------- LANGUAGE → QUALITY ----------------

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):

        _, uid, series, season, lang = q.data.split("|")

        if not await check_owner(q, uid):
            return

        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))

        poster, year, genre, rating = await get_series_info(series)

        caption = build_caption(
            series, year, genre, rating,
            languages=[lang],
            qualities=qs,
            step="quality"
        )

        kb = build_grid_keyboard(
            qs,
            lambda x: f"gqs|{uid}|{series}|{season}|{lang}|{x}"
        )

        await q.message.edit_caption(
            caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb
        )

        await q.answer()

    # ---------------- QUALITY BUTTON ----------------

    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):

        _, uid, series, season, lang, quality = q.data.split("|")

        if not await check_owner(q, uid):
            return

        payload = encode_payload(series, season, lang, quality, uid)

        bot = await cyber.get_me()
        link = f"https://t.me/{bot.username}?start={payload}"

        await q.message.edit_caption(
            UI("☘️ Your files are ready!\nTap below to start your download."),
            reply_markup=InlineKeyboardMarkup([
                [BTN("☘️ DOWNLOAD FILES ☘️", url=link)]
            ])
        )

        await q.answer()
