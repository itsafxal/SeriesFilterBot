import base64
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import DEFAULT_POSTER

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

from .poster import get_series_poster_async
from .session_store import set_session
from .delivery import deliver_files_direct


# ======================================================
# HELPERS
# ======================================================

async def resolve_series(query: str):
    query = spell_correct(clean_query(query))

    rows = await search_series(query)
    if not rows:
        return None

    titles = [clean_series_name(x) for x in rows]
    return fuzzy_match(query, titles) or strict_match_only(query, titles)


def _encode_payload(series, season, lang, quality, uid):
    raw = f"{uid}|{series}|{season}|{lang}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"send_{b64}"


def _decode_payload(payload):
    b64 = payload.replace("send_", "")
    pad = "=" * (-len(b64) % 4)
    return base64.urlsafe_b64decode(b64 + pad).decode().split("|")


# ======================================================
# REGISTER
# ======================================================

def register_filters(cyber: Client):

    # ================= START HANDLER (DEEP LINK) =================

    @cyber.on_message(filters.command("start") & filters.private)
    async def start_handler(_, m: Message):

        if len(m.command) < 2:
            return await m.reply("Send a series name or use buttons from group.")

        payload = m.command[1]
        if not payload.startswith("send_"):
            return

        uid, series, season, lang, quality = _decode_payload(payload)

        if m.from_user.id != int(uid):
            return await m.reply("❌ This link is not for you.")

        set_session(m.from_user.id, {
            "series": series,
            "season": int(season),
            "language": lang,
            "quality": quality
        })

        await m.reply("📤 Sending your files…")
        await deliver_files_direct(cyber, m.from_user.id)


    # ================= PRIVATE SEARCH =================

    @cyber.on_message(filters.text & filters.private)
    async def pm_search(_, m: Message):

        series = await resolve_series(m.text)
        if not series:
            return await m.reply("No result")

        summary = await get_series_summary(series)

        kb = [[InlineKeyboardButton(f"Season {s}", callback_data=f"ss|{series}|{s}")]
              for s in summary["seasons"]]

        await m.reply(series, reply_markup=InlineKeyboardMarkup(kb))


    # ================= GROUP SEARCH =================

    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m: Message):

        if not m.text or m.text.startswith("/"):
            return

        series = await resolve_series(m.text)
        if not series:
            return

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


    # ================= PRIVATE FLOW =================

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

        await q.answer("Sending…")
        await deliver_files_direct(cyber, q.from_user.id)


    # ================= GROUP FLOW =================

    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q):
        _, uid, series, season = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        langs = await get_unique_languages_by_season(series, int(season))
        kb = [[InlineKeyboardButton(l, callback_data=f"gls|{uid}|{series}|{season}|{l}")] for l in langs]

        await q.message.edit_caption("Select Language", reply_markup=InlineKeyboardMarkup(kb))


    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q):
        _, uid, series, season, lang = q.data.split("|")
        if q.from_user.id != int(uid):
            return

        qs = await get_unique_qualities_by_lang_season(series, lang, int(season))
        kb = [[InlineKeyboardButton(x, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{x}")] for x in qs]

        await q.message.edit_caption("Select Quality", reply_markup=InlineKeyboardMarkup(kb))


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
