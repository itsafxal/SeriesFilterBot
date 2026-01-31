# CyberTG/filters.py

import math
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

from config import DEFAULT_POSTER
from DB.database import (
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)

from utils.series_helper import resolve_series, suggest_series
from utils.telegram_helper import delete_message_later
from utils.filter_helper import encode_payload

from .poster import get_series_info
from .files import set_session, deliver_files_direct


SEASONS_PER_PAGE = 9


def build_caption(series, year, genre, rating, seasons=None, languages=None, qualities=None, step="season"):
    lines = [
        f"○ Title: {series}",
        f"○ Released On: {year or 'N/A'}",
        f"○ Genre: {genre or 'N/A'}",
        f"○ Rating: {rating or 'N/A'}",
        ""
    ]

    if step == "season":
        if seasons:
            season_txt = ", ".join(f"S{s}" for s in seasons)
            lines.append(f"○ Available Seasons: {season_txt}")
        lines.append("")
        lines.append("Select an available season...!")

    elif step == "language":
        lang_txt = ", ".join(languages) if languages else "Unknown"
        lines.append(f"○ Available Languages: {lang_txt}")
        lines.append("")
        lines.append("Select an available language...!")

    elif step == "quality":
        lang_txt = ", ".join(languages) if languages else "Unknown"
        qual_txt = ", ".join(qualities) if qualities else ""
        lines.append(f"○ Available Languages: {lang_txt}")
        lines.append(f"○ Available Quality: {qual_txt}")
        lines.append("")
        lines.append("Select an available quality...!")

    return "\n".join(lines)


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
        row.append(InlineKeyboardButton(f"S{s}", callback_data=f"gss|{uid}|{series}|{s}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"spg|{uid}|{series}|{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"spg|{uid}|{series}|{page+1}"))

    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


def register_filters(cyber: Client):

    async def show_suggestions(message, suggestions):
        try:
            await message.delete()
        except:
            pass

        kb = [[InlineKeyboardButton(x, callback_data=f"sug|{x}")] for x in suggestions]
        sent = await cyber.send_message(
            message.chat.id,
            "❓ Did you mean:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        await delete_message_later(cyber, sent.chat.id, sent.id, 40)


    # ---------------- PRIVATE SEARCH ----------------

    @cyber.on_message(filters.text & filters.private)
    async def pm_search(_, m):
        series = await resolve_series(m.text)
        if not series:
            sugg = await suggest_series(m.text)
            if sugg:
                return await show_suggestions(m, sugg)
            return await m.reply("❌ No result")

        poster, year, genre, rating = await get_series_info(series)
        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])

        caption = build_caption(series, year, genre, rating, seasons=seasons, step="season")
        kb = build_season_pages(seasons, m.from_user.id, series, 0)

        await m.reply_photo(poster or DEFAULT_POSTER, caption=caption, reply_markup=kb)


    # ---------------- GROUP SEARCH ----------------

    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m):
        if not m.text or m.text.startswith("/"):
            return

        series = await resolve_series(m.text)
        if not series:
            sugg = await suggest_series(m.text)
            if sugg:
                return await show_suggestions(m, sugg)
            return

        poster, year, genre, rating = await get_series_info(series)
        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])
        uid = m.from_user.id

        caption = build_caption(series, year, genre, rating, seasons=seasons, step="season")
        kb = build_season_pages(seasons, uid, series, 0)

        await m.reply_photo(
            poster or DEFAULT_POSTER,
            caption=caption,
            reply_markup=kb,
            parse_mode=enums.ParseMode.MARKDOWN
        )


    # ---------------- SUGGESTION CLICK ----------------

    @cyber.on_callback_query(filters.regex("^sug\\|"))
    async def sug(_, q):
        series = q.data.split("|", 1)[1]
        uid = q.from_user.id

        poster, year, genre, rating = await get_series_info(series)
        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])

        caption = build_caption(series, year, genre, rating, seasons=seasons, step="season")
        kb = build_season_pages(seasons, uid, series, 0)

        try:
            await q.message.edit_media(InputMediaPhoto(poster, caption=caption), reply_markup=kb)
        except:
            await q.message.edit_caption(caption, reply_markup=kb)

        await delete_message_later(cyber, q.message.chat.id, q.message.id, 10)
        await q.answer()


    # ---------------- SEASON PAGINATION ----------------

    @cyber.on_callback_query(filters.regex("^spg\\|"))
    async def season_page(_, q):
        _, uid, series, page = q.data.split("|")
        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])

        kb = build_season_pages(seasons, uid, series, int(page))
        await q.message.edit_reply_markup(kb)
        await q.answer()


    @cyber.on_callback_query(filters.regex("^noop$"))
    async def noop(_, q):
        await q.answer()


    # ---------------- SEASON -> LANGUAGE ----------------

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|")
        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        langs = await get_unique_languages_by_season(series, int(season))
        poster, year, genre, rating = await get_series_info(series)

        caption = build_caption(series, year, genre, rating, languages=langs, step="language")

        kb = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{series}|{season}|{l}", per_row=2)
        kb.inline_keyboard.append([InlineKeyboardButton("⬅ Back", callback_data=f"back_season|{uid}|{series}")])

        await q.message.edit_caption(caption, reply_markup=kb)
        await q.answer()


    # ---------------- LANGUAGE -> QUALITY ----------------

    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|")
        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))
        poster, year, genre, rating = await get_series_info(series)

        caption = build_caption(series, year, genre, rating, languages=[lang], qualities=qs, step="quality")

        kb = build_grid_keyboard(qs, lambda x: f"gqs|{uid}|{series}|{season}|{lang}|{x}", per_row=2)
        kb.inline_keyboard.append([InlineKeyboardButton("⬅ Back", callback_data=f"back_lang|{uid}|{series}|{season}")])

        await q.message.edit_caption(caption, reply_markup=kb)
        await q.answer()


    # ---------------- BACK BUTTONS ----------------

    @cyber.on_callback_query(filters.regex("^back_season\\|"))
    async def back_season(_, q):
        _, uid, series = q.data.split("|")
        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        summary = await get_series_summary(series)
        seasons = list(summary["seasons"])
        kb = build_season_pages(seasons, uid, series, 0)

        await q.message.edit_reply_markup(kb)
        await q.answer()


    @cyber.on_callback_query(filters.regex("^back_lang\\|"))
    async def back_lang(_, q):
        _, uid, series, season = q.data.split("|")
        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        langs = await get_unique_languages_by_season(series, int(season))

        kb = build_grid_keyboard(langs, lambda l: f"gls|{uid}|{series}|{season}|{l}", per_row=2)
        kb.inline_keyboard.append([InlineKeyboardButton("⬅ Back", callback_data=f"back_season|{uid}|{series}")])

        await q.message.edit_reply_markup(kb)
        await q.answer()


    # ---------------- QUALITY -> PM DELIVERY ----------------

    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q):
        _, uid, series, season, lang, quality = q.data.split("|")

        if q.from_user.id != int(uid):
            return await q.answer("Not your menu", show_alert=True)

        set_session(uid, {
            "series": series,
            "season": int(season),
            "language": lang,
            "quality": quality
        })

        try:
            await deliver_files_direct(cyber, uid)
            try:
                await q.message.edit_reply_markup(None)
            except:
                pass
            await q.answer("📥 Sending files in PM…")

        except Exception:
            payload = encode_payload(series, season, lang, quality, uid)
            bot = await cyber.get_me()
            link = f"https://t.me/{bot.username}?start={payload}"

            try:
                await q.message.edit_caption(
                    "👉 Start bot in PM first to receive files.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶ Start Bot", url=link)]])
                )
            except:
                pass
