# groupfilter.py
import asyncio
import base64
import re
import asyncio
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

from config import (
    logger,
    DEFAULT_POSTER,
    MAX_CALLBACK_LENGTH,
    OMDB_CACHE_DB,
    SPELL_IMG,
)

from utils import clean_series_name, delete_message_later
from DB.database import (
    get_global_setting,
    search_series,
    get_series_summary,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)
from utils import delete_message_later
from .poster import get_series_poster_async

try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False


# ---------------------------------------------------------
# BUILD POSTER
# ---------------------------------------------------------
async def _build_poster_for_series(series_name: str):
    poster = DEFAULT_POSTER
    try:
        cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
        with shelve.open(cache_file) as cache:
            key = f"poster::{series_name.lower()}"
            if key in cache:
                poster = cache[key]
    except Exception as e:
        logger.error(f"Error reading OMDB cache: {e}")

    try:
        new = await get_series_poster_async(series_name)
        if new:
            poster = new
    except Exception as e:
        logger.error(f"Error fetching new series poster for {series_name}: {e}")

    return poster

async def _check_owner(q: CallbackQuery, owner_id: int):
    if not q.from_user or q.from_user.id != owner_id:
        await q.answer(
            "❌ This result is not for you.\nPlease search your own request.",
            show_alert=True
        )
        return False
    return True

# ---------------------------------------------------------
# DEEP LINK
# ---------------------------------------------------------
def _encode_payload(series, season, lang, quality, owner_id):
    raw = f"{owner_id}|{series}|{season}|{lang}|{quality}"
    b64 = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"send_{quote(b64)}"


# ---------------------------------------------------------
# CLEAN QUERY
# ---------------------------------------------------------
def _clean_query(q: str): 
    return re.sub( 
        r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\sme)?)|movie(s)?|new|" 
        r"latest|br((o|u)h?)*|^h(e|a)?(l)*(o)*|mal(ayalam)?|t(h)?amil|file|that|find|und(o)*|" 
        r"kit(t(i|y)?)?o(w)?|thar(u)?(o)*w?|kittum(o)*|aya(k)*(um(o)*)?|full\smovie|any(one)|" 
        r"with\ssubtitle(s)?)", "", 
        q, flags=re.IGNORECASE, ).strip()

# ---------------------------------------------------------
# FUZZY MATCHING
# ---------------------------------------------------------
def _fuzzy_match(query: str, candidates: list, limit=5):
    if not candidates:
        return []

    if HAVE_RAPIDFUZZ:
        res = rf_process.extract(
            query,
            candidates,
            scorer=rf_fuzz.ratio,
            limit=limit
        )
        return [(x[0], x[1]) for x in res if x[1] >= 30]
    else:
        # basic fallback
        scored = []
        ql = query.lower()
        for c in candidates:
            cl = c.lower()
            # Simple character match score (very basic)
            m = sum(ch in cl for ch in ql)
            score = (m / (len(ql) + len(cl))) * 200
            if score >= 30:
                scored.append((c, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]


# ---------------------------------------------------------
# STRICT MATCH CHECK (NEW: ONLY EXACT MATCH ALLOWED)
# ---------------------------------------------------------
def _strict_match_only(query: str, names: list):
    """
    STRICT match ONLY if EXACT match.
    """
    q = query.lower().strip()
    matches = []

    for n in names:
        if n.lower().strip() == q:    # ONLY exact match
            matches.append(n)

    return matches

# ---------------------------------------------------------
# MAIN REGISTRATION
# ---------------------------------------------------------
def register_groupfilter_handlers(cyber: Client):

    # -----------------------------------------------------
    # GROUP TEXT SEARCH
    # -----------------------------------------------------
    @cyber.on_message(filters.text & filters.group)
    async def group_search(_, m: Message):

        text = (m.text or "").strip()
        if not text or len(text) < 2:
            return

        if text.startswith("/") or "http" in text:
            return

        # 1) initial DB search
        try:
            results = await search_series(text)
        except Exception as e:
            logger.error(f"DB search failed for query '{text}': {e}")
            results = []

        results = [clean_series_name(r) for r in results]

        # 2) STRICT MATCH (original query)
        strict = _strict_match_only(text, results)

        if strict:
            # auto forwarding if exact match
            series = strict[0]
            return await _send_series_entry(cyber, m, series)

        # 3) CLEANED query search
        cleaned = _clean_query(text)
        cleaned_results = []

        if cleaned:
            try:
                cleaned_results = await search_series(cleaned)
            except Exception as e:
                logger.error(f"DB search failed for cleaned query '{cleaned}': {e}")
                pass

        cleaned_results = [clean_series_name(r) for r in cleaned_results]

        # 4) STRICT MATCH (cleaned query)
        strict2 = _strict_match_only(cleaned, cleaned_results)
        if strict2:
            series = strict2[0]
            return await _send_series_entry(cyber, m, series)

        # 5) fuzzy suggestions
        candidates = list({*results, *cleaned_results})
        if not candidates:
            return await m.reply_text(
                f"❌ No series found `<code>{text}</code>`",
                parse_mode=enums.ParseMode.HTML
            )

        fuzzy = _fuzzy_match(text, candidates, limit=10)
        if not fuzzy:
            return await m.reply_text(
                f"❌ No series found `<code>{text}</code>`",
                parse_mode=enums.ParseMode.HTML
            )

        # top 3 suggestions
        suggestions = []
        for n, s in fuzzy:
            if n not in suggestions:
                suggestions.append(n)
            if len(suggestions) >= 3:
                break

        # suggestion UI
        uid = m.from_user.id if m.from_user else 0
        kb = [[InlineKeyboardButton(s, callback_data=f"gsugg|{uid}|{s}")]
              for s in suggestions]
        kb.append([InlineKeyboardButton("Close", callback_data=f"close_suggestion|{uid}")])
        await m.reply_photo(
            photo=SPELL_IMG,
            caption=(
                f"I couldn't find anything related to <code>{text}</code>.\n"
                f"Did you mean any of these?"
            ),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.HTML
        )
        return

    # -----------------------------------------------------
    # SUGGESTION SELECT
    # -----------------------------------------------------
    @cyber.on_callback_query(filters.regex("^gsugg\\|"))
    async def suggestion_select(_, q: CallbackQuery):
        _, uid, series = q.data.split("|", 2)
        uid = int(uid)
        if not await _check_owner(q, uid):
            return
        # Optional check if user matches uid, omitted for brevity/simplicity
        await q.answer(f"Selected: {series}")
        return await _edit_to_series_entry(cyber, q, series)

    @cyber.on_callback_query(filters.regex("^close_suggestion\\|"))
    async def suggestion_close(c, q: CallbackQuery):
        try:
            await q.message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete suggestion message: {e}")
            pass
        await q.answer()


    # -----------------------------------------------------
    # gss -> gls -> gqs (Group Season -> Language -> Quality Select)
    # -----------------------------------------------------
    @cyber.on_callback_query(filters.regex("^gss\\|"))
    async def gss(_, q: CallbackQuery):
        _, uid, series, season = q.data.split("|")
        uid = int(uid)
        season = int(season)
        if not await _check_owner(q, uid):
            return

        langs = await get_unique_languages_by_season(series, season)
        if not langs:
            return await q.answer("No languages found", show_alert=True)

        kb = [
            [InlineKeyboardButton(l, callback_data=f"gls|{uid}|{series}|{season}|{l}")]
            for l in langs
        ]
        # Back button is changed to point to the main series entry
        kb.append([InlineKeyboardButton("↺ Back", callback_data=f"gback|{uid}|{series}")])

        try:
            await q.message.edit_caption( # Use edit_caption if the message is a photo
                f"📺 Title **{series}  — Season {season}**\n\nExcellent choice!✨ {series} is a fantastic show. I hope, you will enjoy this TV Series\n\n🌐 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗔𝘂𝗱𝗶𝗼 𝗟𝗮𝗻𝗴𝘂𝗮𝗴𝗲 👇",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
             await q.message.edit_text( # Fallback to edit_text if it fails
                f"📺 Title **{series}  — Season {season}**\n\nExcellent choice!✨ {series} is a fantastic show. I hope, you will enjoy this TV Series\n\n🌐 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗔𝘂𝗱𝗶𝗼 𝗟𝗮𝗻𝗴𝘂𝗮𝗴𝗲 👇",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        await q.answer()


    @cyber.on_callback_query(filters.regex("^gls\\|"))
    async def gls(_, q: CallbackQuery):
        _, uid, series, season, lang = q.data.split("|")
        uid = int(uid)
        season = int(season)
        if not await _check_owner(q, uid):
            return

        qualities = await get_unique_qualities_by_lang_season(series, lang, season)
        if not qualities:
            return await q.answer("No qualities found", show_alert=True)

        kb = [
            [InlineKeyboardButton(qy, callback_data=f"gqs|{uid}|{series}|{season}|{lang}|{qy}")]
            for qy in qualities
        ]
        # Back button is changed to point to the language selection menu (gss)
        kb.append([InlineKeyboardButton("↺ Back", callback_data=f"gss|{uid}|{series}|{season}")])

        try:
            await q.message.edit_caption( # Use edit_caption if the message is a photo
                f"🎬 {series} – Season {season} ({lang})\n\n📌 Quality Selector\n\nTap a quality below —\n▶️ Bot will instantly auto-send the **WHOLE SEASON** (all episodes) in that same resolution.\n\n>⚡ Lightning Fast\n>📦 Full Season Pack\n>🎥 Download And Watch:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
             await q.message.edit_text( # Fallback to edit_text if it fails
                f"🎬 {series} – Season {season} ({lang})\n\n📌 Quality Selector\n\nTap a quality below —\n▶️ Bot will instantly auto-send the **WHOLE SEASON** (all episodes) in that same resolution.\n\n>⚡ Lightning Fast\n>📦 Full Season Pack\n>🎥 Download And Watch:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=enums.ParseMode.MARKDOWN
            )
        await q.answer()


    @cyber.on_callback_query(filters.regex("^gqs\\|"))
    async def gqs(_, q: CallbackQuery):
        # --- NEW/MODIFIED CODE: Fetching the timer ---
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0) 
        # --- END OF NEW/MODIFIED CODE ---
         
        _, uid, series, season, lang, quality = q.data.split("|")
        uid = int(uid)
        season = int(season)
        if not await _check_owner(q, uid):
            return

        # Fetch bot username dynamically
        bot = await cyber.get_me()
        param = _encode_payload(series, season, lang, quality, uid)
        link = f"https://t.me/{bot.username}?start={param}"

        # 1. Redirect the user directly via query.answer(url=...)
        await q.answer(
            "Tap OK/Open to receive the files in private chat.", 
            url=link, 
            show_alert=False
        )
        kb = [
            [InlineKeyboardButton("📥 Get Your Files", url=link)],
            [
            InlineKeyboardButton("↺ Back to Quality", callback_data=f"gls|{uid}|{series}|{season}|{lang}")
            ]
        ]
        # 2. Edit the message to let the user know what happened
        edited_message = q.message # Default fallback
        try:
            # Use edit_caption if the message is a photo
            edited_message = await q.message.edit_caption( 
                f"✔ **{series} — S{season} ({lang}) [{quality}]**\n\n"
                f"Files are now being delivered in the private chat of the bot. **Check your DMs!**",
                reply_markup=InlineKeyboardMarkup(kb), # Remove buttons
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.warning(f"Failed to edit gqs message after redirect: {e}")
            try:
                # Fallback to edit_text
                edited_message = await q.message.edit_text(
                    f"✔ **{series} — S{season} ({lang}) [{quality}]**\n\n"
                    f"Files are now being delivered in the private chat of the bot. **Check your DMs!**",
                    reply_markup=InlineKeyboardMarkup(kb), # Remove buttons
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception:
                edited_message = q.message # Ensure edited_message is set if both fail
                pass
        
        # Correctly indented block for auto-delete
        if auto_delete_timer > 0 and edited_message:
            asyncio.create_task(
                delete_message_later(
                    cyber,
                    edited_message.chat.id,
                    edited_message.id,
                    auto_delete_timer
                )
            )

    @cyber.on_callback_query(filters.regex("^gback\\|"))
    async def gback(_, q: CallbackQuery):
        _, uid, series = q.data.split("|")

        summary = await get_series_summary(series)
        seasons = summary.get("seasons", [])
        uid = int(uid)
        if not await _check_owner(q, uid):
            return
        kb = [
            [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{uid}|{series}|{s}")]
            for s in seasons
        ]
        
        caption = (
            f"**○ Title:** {series}\n"
            f"**○ Language:** {', '.join(summary.get('languages', [])) or 'N/A'}\n"
            f"**○ Seasons Available:** {len(seasons)}\n"
            f"**○ Quality Available:** {', '.join(summary.get('qualities', [])) or 'N/A'}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )
        
        # Try to edit the media back to the poster view
        poster = await _build_poster_for_series(series)

        try:
            await q.message.edit_media(
                media=InputMediaPhoto(
                    media=poster,
                    caption=caption,
                    parse_mode=enums.ParseMode.MARKDOWN,
                ),
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            # Fallback to edit_text
            logger.error(f"Failed to edit_media for gback (will use text fallback): {e}")
            await q.message.edit_text(
                caption,
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            
        await q.answer()


# ---------------------------------------------------------
# Sends the initial series entry (with poster and options)
# ---------------------------------------------------------
async def _send_series_entry(cyber, m: Message, series: str):
    poster = await _build_poster_for_series(series)
    summary = await get_series_summary(series)

    seasons = summary.get("seasons", [])
    languages_list = ", ".join(summary.get("languages", []))
    qualities_list = ", ".join(summary.get("qualities", []))
    seasons_count = len(seasons)

    # Collect ALL languages and qualities (redundant but kept for robust display)
    all_langs = set()
    all_qualities = set()

    for s in seasons:
        try:
            langs = await get_unique_languages_by_season(series, s)
            for L in langs:
                all_langs.add(L)
                ql = await get_unique_qualities_by_lang_season(series, L, s)
                all_qualities.update(ql)
        except Exception as e:
            logger.warning(f"Error gathering season data for {series} S{s}: {e}")

    caption = (
        f"**○ Title:** {series}\n"
        f"**○ Language:** {languages_list or 'N/A'}\n"
        f"**○ Seasons Available:** {seasons_count}\n"
        f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
        f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
    )
    uid = m.from_user.id
    # season buttons
    kb = [
        [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{uid}|{series}|{s}")]
        for s in seasons
    ]

    sent = None # Initialize sent message variable
    try:
        sent = await m.reply_photo(
            poster,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Failed to reply_photo for {series}, using text fallback: {e}")
        sent = await m.reply_text(
            caption,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        
    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
    if auto_delete_timer > 0 and sent:
        asyncio.create_task(
            delete_message_later(cyber, sent.chat.id, sent.id, auto_delete_timer)
        )

    return sent

# ---------------------------------------------------------
# EDIT FROM SUGGESTION -> FULL SERIES ENTRY (WITH CAPTION AND POSTER)
# ---------------------------------------------------------
async def _edit_to_series_entry(cyber, q: CallbackQuery, series: str):

    poster = await _build_poster_for_series(series)
    summary = await get_series_summary(series)
    seasons = summary.get("seasons", [])
    languages_list = ", ".join(summary.get("languages", []))
    qualities_list = ", ".join(summary.get("qualities", []))
    seasons_count = len(seasons)

    # Collect ALL languages and qualities (same as _send_series_entry)
    all_langs = set()
    all_qualities = set()

    for s in seasons:
        try:
            langs = await get_unique_languages_by_season(series, s)
            for L in langs:
                all_langs.add(L)
                ql = await get_unique_qualities_by_lang_season(series, L, s)
                all_qualities.update(ql)
        except Exception as e:
            logger.warning(f"Error gathering season data for {series} S{s}: {e}")

    # Build caption (same as _send_series_entry)
    caption = (
        f"**○ Title:** {series}\n"
        f"**○ Language:** {languages_list or 'N/A'}\n"
        f"**○ Seasons Available:** {seasons_count}\n"
        f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
        f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
    )
    uid = q.from_user.id

    # Build season buttons
    kb = [
        [InlineKeyboardButton(f"Season {s}", callback_data=f"gss|{uid}|{series}|{s}")]
        for s in seasons
    ]

    sent = q.message # Set initial message object
    # EDIT MESSAGE → MUST use edit_media + InputMediaPhoto to change from text to photo
    try:
        sent = await q.message.edit_media(
            media=InputMediaPhoto(
                media=poster,
                caption=caption,
                parse_mode=enums.ParseMode.MARKDOWN,
            ),
            reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception as e:
        # Fallback (message was not a photo or media edit failed)
        logger.error(f"Failed to edit_media for {series} (will use text fallback): {e}")
        sent = await q.message.edit_text(
            caption,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=enums.ParseMode.MARKDOWN
        )
        
    auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
    # The fix for IndentationError starts here:
    if auto_delete_timer > 0 and sent:
        asyncio.create_task(
            delete_message_later(cyber, sent.chat.id, sent.id, auto_delete_timer)
        )

    await q.answer()
