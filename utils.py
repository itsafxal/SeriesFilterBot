# ======================================================
# utils.py — FULL VERSION WITH RTI NOTIFICATIONS (NO MISSING PARTS)
# ======================================================

import asyncio
import logging
import re
import aiohttp
import shelve

from pyrogram import Client, enums, types
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import (
    logger,
    DEFAULT_POSTER,
    OMDB_API_KEY,
    NEW_SERIES_NOTIFY_CHANNEL,
)

from DB.database import (
    save_file_to_db,
    get_all_files_by_season_and_quality,
    get_global_setting,
)

from CyberTG.poster import get_series_poster_async

logging.basicConfig(level=logging.INFO)

# ======================================================
# RTI GLOBAL STATE
# ======================================================

RTI_NOTIFY_BUFFER = {}        # {series: {season: set(episodes)}}
RTI_LANGUAGE_BUFFER = {}      # {series: set(languages)}
RTI_NOTIFIED_SEASONS = {}     # {series: set(seasons_already_notified)}
RTI_NOTIFY_TASK = None
RTI_NOTIFY_DELAY = 30         # seconds

# ======================================================
# TELEGRAM LINK EXTRACTION
# ======================================================

PRIVATE_LINK_PATTERN = r"https://t\.me/c/(\d+)/(\d+)"
PUBLIC_LINK_PATTERN = r"https://t\.me/([A-Za-z0-9_]+)/(\d+)"
JOINCHAT_PATTERN = r"https://t\.me/\+([A-Za-z0-9_-]+)"


def _resolve_public_channel_id(client: Client, username: str):
    try:
        return client.get_chat(username).id
    except Exception:
        return None


def extract_links(links, client=None):
    def parse(link: str):
        m = re.match(PRIVATE_LINK_PATTERN, link)
        if m:
            return int(f"-100{m.group(1)}"), int(m.group(2))

        m = re.match(PUBLIC_LINK_PATTERN, link)
        if m:
            if not client:
                raise ValueError("Client required")
            cid = _resolve_public_channel_id(client, m.group(1))
            if not cid:
                raise ValueError("Invalid channel")
            return cid, int(m.group(2))

        if re.match(JOINCHAT_PATTERN, link):
            raise ValueError("Joinchat links not supported")

        raise ValueError("Invalid Telegram link")

    if len(links) == 1:
        cid, end = parse(links[0])
        return cid, 2, end

    if len(links) == 2:
        c1, s = parse(links[0])
        c2, e = parse(links[1])
        if c1 != c2:
            raise ValueError("Links must match")
        return c1, s, e

    raise ValueError("Provide 1 or 2 links only")

# ======================================================
# SMALL CAPS
# ======================================================

SMALL_CAPS_MAP = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
)


def to_small_caps(text: str) -> str:
    return text.translate(SMALL_CAPS_MAP)

# ======================================================
# SERIES PARSING
# ======================================================


def clean_series_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.title().strip()


def extract_quality(name: str) -> str:
    name = name.upper()
    if "2160P" in name or "4K" in name:
        return "4K"
    if "1080P" in name:
        return "1080P"
    if "720P" in name:
        return "720P"
    return "480P"


def extract_language(name: str) -> str:
    name = name.upper()
    if "HINDI" in name:
        return "Hindi"
    if "TAMIL" in name:
        return "Tamil"
    if "TELUGU" in name:
        return "Telugu"
    if "MALAYALAM" in name:
        return "Malayalam"
    return "English"


def parse_series_info(file_name: str):
    match = re.search(r"[sS](\d+)[.\-_ ]*[eE](\d+)", file_name)
    quality = extract_quality(file_name)
    language = extract_language(file_name)

    if not match:
        return clean_series_name(file_name), 0, 0, quality, language

    season = int(match.group(1))
    episode = int(match.group(2))
    series = clean_series_name(file_name[:match.start()])

    return series, season, episode, quality, language

# ======================================================
# AUDIO LABEL
# ======================================================


def get_audio_label(langs: set):
    if not langs:
        return "ᴜɴᴋɴᴏᴡɴ"
    langs = sorted(to_small_caps(l) for l in langs)
    if len(langs) == 1:
        return langs[0]
    if len(langs) == 2:
        return f"ᴅᴜᴀʟ-ᴀᴜᴅɪᴏ ({', '.join(langs)})"
    return f"ᴍᴜʟᴛɪ-ᴀᴜᴅɪᴏ ({', '.join(langs)})"

# ======================================================
# OMDB FETCH
# ======================================================

OMDB_CACHE = {}


async def fetch_imdb_info(title: str):
    if title in OMDB_CACHE:
        return OMDB_CACHE[title]

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://www.omdbapi.com/",
                params={"t": title, "type": "series", "apikey": OMDB_API_KEY},
                timeout=10
            ) as r:
                data = await r.json()
    except Exception:
        data = {}

    if data.get("Response") != "True":
        info = {"genre": "n/a", "rating": "n/a"}
    else:
        info = {
            "genre": data.get("Genre", "n/a"),
            "rating": data.get("imdbRating", "n/a")
        }

    OMDB_CACHE[title] = info
    return info

# ======================================================
# FORCE SUBSCRIBE
# ======================================================


async def delete_message_later(cyber: Client, chat_id, msg_id, delay):
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    try:
        await cyber.delete_messages(chat_id, msg_id)
    except Exception:
        pass


async def check_force_subscribe(cyber: Client, user_id: int):
    channel = await get_global_setting("FORCE_SUB_CHANNEL_ID", None)
    if not channel:
        return True, None
    try:
        m = await cyber.get_chat_member(channel, user_id)
        if m.status in (
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER
        ):
            return True, None
    except Exception:
        pass
    try:
        return False, (await cyber.get_chat(channel)).invite_link
    except Exception:
        return False, None

# ======================================================
# RTI BUFFER
# ======================================================


def buffer_rti_episode(series, season, episode, language):
    RTI_NOTIFY_BUFFER.setdefault(series, {})
    RTI_LANGUAGE_BUFFER.setdefault(series, set())
    RTI_NOTIFIED_SEASONS.setdefault(series, set())

    RTI_LANGUAGE_BUFFER[series].add(language)

    if season in RTI_NOTIFIED_SEASONS[series]:
        return False

    RTI_NOTIFY_BUFFER[series].setdefault(season, set()).add(episode)
    return True

# ======================================================
# RTI FLUSH
# ======================================================


async def flush_rti_notifications(bot: Client):
    for series, seasons in RTI_NOTIFY_BUFFER.items():
        imdb = await fetch_imdb_info(series)

        title = f"**{to_small_caps(series)} {to_small_caps('Added')}**"
        audio = get_audio_label(RTI_LANGUAGE_BUFFER.get(series))

        audio_block = (
            f"🎙️ {to_small_caps('Audio')}: {audio}\n\n"
            if audio != "ᴜɴᴋɴᴏᴡɴ" else ""
        )

        imdb_block = (
            f"⭐ {to_small_caps('IMDB Rating')}: {imdb['rating']}\n\n"
            if imdb.get("rating") and imdb["rating"].lower() != "n/a" else ""
        )

        season_lines = []
        for s in sorted(seasons):
            eps = sorted(seasons[s])
            if len(eps) == 1:
                ep = f"ᴇᴘɪsᴏᴅᴇ {eps[0]}"
            else:
                ep = f"ᴇᴘɪsᴏᴅᴇs {eps[0]}–{eps[-1]}"
            season_lines.append(f"• {to_small_caps('Season')} {s} → {ep}")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔍 ɢᴇᴛ ꜰɪʟᴇs",
                callback_data=f"rti_search|{series}"
            )]
        ])

        text = (
            f"✅ {title}\n\n"
            f"🎬 {to_small_caps('Type')}: {to_small_caps('TV Series')}\n\n"
            f"{audio_block}{imdb_block}"
            f"📺 {to_small_caps('Seasons Added')}:\n" +
            "\n".join(season_lines)
        )

        await bot.send_message(
            NEW_SERIES_NOTIFY_CHANNEL,
            text,
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )

        RTI_NOTIFIED_SEASONS[series].update(seasons.keys())

    RTI_NOTIFY_BUFFER.clear()
    RTI_LANGUAGE_BUFFER.clear()

# ======================================================
# RTI SCHEDULER
# ======================================================


async def schedule_rti_flush(bot: Client):
    global RTI_NOTIFY_TASK

    if RTI_NOTIFY_TASK and not RTI_NOTIFY_TASK.done():
        RTI_NOTIFY_TASK.cancel()

    async def delayed():
        try:
            await asyncio.sleep(RTI_NOTIFY_DELAY)
            await flush_rti_notifications(bot)
        except asyncio.CancelledError:
            pass

    RTI_NOTIFY_TASK = asyncio.create_task(delayed())

# ======================================================
# SAVE HOOK (USED BY index.py)
# ======================================================


async def save_series_file_for_bulk_index(message: types.Message):
    file_name = ""
    file_id = None

    if message.document:
        file_name = message.document.file_name or ""
        file_id = message.document.file_id
    elif message.video:
        file_name = message.video.file_name or ""
        file_id = message.video.file_id

    if not file_name and message.caption:
        file_name = message.caption.split("\n")[0]

    if not file_name:
        return False, 0

    series, season, episode, quality, language = parse_series_info(file_name)

    if season == 0 and episode == 0:
        return False, 0

    existing = await get_all_files_by_season_and_quality(
        series, season, quality, language
    )
    if any(r["episode"] == episode for r in existing):
        return False, 0

    poster = await get_series_poster_async(series) or DEFAULT_POSTER

    await save_file_to_db({
        "channel_id": message.chat.id,
        "message_id": message.id,
        "file_name": file_name,
        "normalized_name": series,
        "season": season,
        "episode": episode,
        "quality": quality,
        "language": language,
        "file_id": file_id,
        "poster": poster,
        "timestamp": message.date
    })

    is_new = buffer_rti_episode(series, season, episode, language)
    if is_new:
        await schedule_rti_flush(message._client)

    return True, 1
