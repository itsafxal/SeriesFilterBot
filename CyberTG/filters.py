import math

from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup,
    InputMediaPhoto
)

from config import (
    DEFAULT_POSTER,
    SPELL_IMG,
    SEASONS_PER_PAGE
)

from DB.database import (
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season
)

from utils.series_helper import check_owner
from utils.filter_helper import encode_payload
from utils.font_helper import BTN, UI, small_caps

from .poster import get_series_info


# =================================================
# SMART SEARCH
# =================================================

async def resolve_series_with_suggestions(text):

    text = text.strip().lower()
    results = await search_series(text)

    if not results:
        return None, []

    unique = list(dict.fromkeys(results))

    exact = [x for x in unique if x.lower() == text]

    if exact and len(unique) > 1:
        return None, unique[:10]

    if exact:
        return exact[0], []

    return None, unique[:10]


# =================================================
# CAPTION BUILDER
# =================================================

def build_caption(series, year, genre, rating,
                  seasons=None,
                  languages=None,
                  qualities=None,
                  step="season"):

    lines = [
        f"<b>○ {small_caps('Title')}: {small_caps(series)}</b>",
        f"<b>○ {small_caps('Released')}: {small_caps(year or 'N/A')}</b>",
        f"<b>○ {small_caps('Genre')}: {small_caps(genre or 'N/A')}</b>",
        f"<b>○ {small_caps('Rating')}: {small_caps(rating or 'N/A')}</b>",
        ""
    ]

    if step == "season":
        if seasons:
            txt = ", ".join(f"S{s}" for s in seasons)
            lines.append(f"<b>○ {small_caps('Seasons')}: {txt}</b>")
        lines.append("")
        lines.append(f"<b>{small_caps('Select Season')}</b>")

    elif step == "language":
        txt = ", ".join(languages)
        lines.append(f"<b>○ {small_caps('Languages')}: {small_caps(txt)}</b>")
        lines.append("")
        lines.append(f"<b>{small_caps('Select Language')}</b>")

    elif step == "quality":
        lt = ", ".join(languages)
        qt = ", ".join(qualities)
        lines.append(f"<b>○ {small_caps('Language')}: {small_caps(lt)}</b>")
        lines.append(f"<b>○ {small_caps('Quality')}: {small_caps(qt)}</b>")
        lines.append("")
        lines.append(f"<b>{small_caps('Select Quality')}</b>")

    return "\n".join(lines)


# =================================================
# KEYBOARD HELPERS
# =================================================

def build_grid_keyboard(items, cb_builder, per_row=2):

    rows, row = [], []

    for it in items:
        row.append(BTN(it, callback_data=cb_builder(it)))

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

        nav.append(BTN(f"{page+1}/{total_pages}", callback_data="noop"))

        if page < total_pages - 1:
            nav.append(
                BTN("Next ▶",
                    callback_data=f"spg|{uid}|{series}|{page+1}")
            )

        rows.append(nav)

    return InlineKeyboardMarkup(rows)


# =================================================
# MAIN REGISTER
# =================================================

def register_filters(cyber: Client):

    # -------------------------------------------------
    # SEARCH
    # -------------------------------------------------
    @cyber.on_message(filters.text & (filters.private | filters.group))
    async def search_handler(_, m):

        if not m.text or m.text.startswith("/"):
            return

        series, suggestions = await resolve_series_with_suggestions(m.text)

        if series:

            poster, year, genre, rating = await get_series_info(series)
            seasons = list((await get_series_summary(series))["seasons"])

            kb = build_season_pages(seasons, m.from_user.id, series)

            await m.reply_photo(
                poster or DEFAULT_POSTER,
                caption=build_caption(
                    series, year, genre, rating,
                    seasons=seasons
                ),
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb
            )
            return

        if suggestions:

            kb = InlineKeyboardMarkup(
                [[BTN(s, callback_data=f"sug|{s}")]
                 for s in suggestions[:8]]
                + [[BTN("Close", callback_data="close_spell")]]
            )

            await m.reply_photo(
                SPELL_IMG,
                caption=UI("Did you mean one of these?"),
                reply_markup=kb
            )

    # -------------------------------------------------
    # SPELL SELECT
    # -------------------------------------------------
    @cyber.on_callback_query(filters.regex("^sug\\|"))
    async def sug(_, q):

        series = q.data.split("|", 1)[1]

        poster, year, genre, rating = await get_series_info(series)
        seasons = list((await get_series_summary(series))["seasons"])

        kb = build_season_pages(seasons, q.from_user.id, series)

        await q.message.edit_media(
            InputMediaPhoto(
                poster or DEFAULT_POSTER,
                caption=build_caption(
                    series, year, genre, rating,
                    seasons=seasons
                ),
                parse_mode=enums.ParseMode.HTML
            ),
            reply_markup=kb
        )

    @cyber.on_callback_query(filters.regex("^close_spell$"))
    async def close_spell(_, q):
        await q.message.delete()

    # -------------------------------------------------
    # PAGINATION
    # -------------------------------------------------
    @cyber.on_callback_query(filters.regex("^spg\\|"))
    async def spg(_, q):

        _, uid, series, page = q.data.split("|")

        if not await check_owner(q, uid):
            return

        seasons = list((await get_series_summary(series))["seasons"])
        kb = build_season_pages(seasons, uid, series, int(page))

        await q.message.edit_reply_markup(kb)

    @cyber.on_callback_query(filters.regex("^noop$"))
    async def noop(_, q):
        await q.answer()

    # -------------------------------------------------
    # SEASON → LANGUAGE
    # -------------------------------------------------
    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):

        _, uid, series, season = q.data.split("|")

        if not await check_owner(q, uid):
            return

        langs = await get_unique_languages_by_season(series, int(season))
        poster, year, genre, rating = await get_series_info(series)

        kb = build_grid_keyboard(
            langs,
            lambda l: f"gls|{uid}|{series}|{season}|{l}"
        )

        await q.message.edit_caption(
            build_caption(
                series, year, genre, rating,
                languages=langs,
                step="language"
            ),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb
        )

    # -------------------------------------------------
    # LANGUAGE → QUALITY
    # -------------------------------------------------
    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):

        _, uid, series, season, lang = q.data.split("|")

        if not await check_owner(q, uid):
            return

        qs = await get_unique_qualities_by_lang_season(
            series, lang, int(season)
        )

        poster, year, genre, rating = await get_series_info(series)

        kb = build_grid_keyboard(
            qs,
            lambda x: f"gqs|{uid}|{series}|{season}|{lang}|{x}"
        )

        await q.message.edit_caption(
            build_caption(
                series, year, genre, rating,
                languages=[lang],
                qualities=qs,
                step="quality"
            ),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb
        )

    # -------------------------------------------------
    # QUALITY → GENERATE PAYLOAD
    # -------------------------------------------------
    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):

        _, uid, series, season, lang, quality = q.data.split("|")

        if not await check_owner(q, uid):
            return

        payload = encode_payload(series, season, lang, quality, uid)

        bot = await cyber.get_me()

        link = f"https://t.me/{bot.username}?start={payload}"

        await q.message.edit_caption(
            UI("☘️ Your files are ready!\nTap below to download."),
            reply_markup=InlineKeyboardMarkup([
                [BTN("☘️ DOWNLOAD FILES ☘️", url=link)]
            ])
        )
