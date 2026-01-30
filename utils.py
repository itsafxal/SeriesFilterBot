# utils.py
import requests
import logging
import asyncio
import re
from pyrogram import types, Client, enums
from config import logger, DEFAULT_POSTER, OMDB_CACHE_DB
from DB.database import save_file_to_db, get_all_files_by_season_and_quality, get_global_setting
from CyberTG.poster import get_series_poster_async
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
logging.basicConfig(level=logging.INFO)

LANGUAGE_CODES = {
    "HIN": "Hindi",
    "ENG": "English",
    "TAM": "Tamil",
    "TEL": "Telugu",
    "MALA": "Malayalam",
    "DUB": "Hindi"  # Often used alone to indicate Hindi dub
}
# Create a pattern string for the regex to match any of the known codes
LANG_CODE_PATTERN = "|".join(LANGUAGE_CODES.keys())

# Regex patterns for detecting various Telegram link types
PRIVATE_LINK_PATTERN = r"https://t\.me/c/(\d+)/(\d+)"
PUBLIC_LINK_PATTERN = r"https://t\.me/([A-Za-z0-9_]+)/(\d+)"
JOINCHAT_PATTERN = r"https://t\.me/\+([A-Za-z0-9_-]+)"

# ================= SEARCH HELPERS =================

try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False


def clean_query(q: str):
    if not q:
        return ""
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def fuzzy_match(query, choices, cutoff=70):
    if not HAVE_RAPIDFUZZ or not choices:
        return None

    match = rf_process.extractOne(
        query,
        choices,
        scorer=rf_fuzz.partial_ratio,
        score_cutoff=cutoff
    )

    if match:
        return match[0]

    return None


def strict_match_only(query, choices):
    q = query.lower()
    for c in choices:
        if q in c.lower():
            return c
    return None

def _resolve_public_channel_id(client, username: str):
    """Helper to resolve public channel username → chat_id."""
    try:
        chat = client.get_chat(username)
        return chat.id
    except Exception:
        return None


def extract_links(links, client=None):
    """
    Extracts chat_id, start_msg_id, and last_msg_id from:
    - Private links:  https://t.me/c/<channel_id>/<msg_id>
    - Public links:   https://t.me/<username>/<msg_id>
    - Joinchat links: https://t.me/+xxxxxx
    """

    def parse_link(link: str):
        """Parses a single link and returns (chat_id, msg_id)."""

        # ----- PRIVATE CHANNEL LINK -----
        match = re.match(PRIVATE_LINK_PATTERN, link)
        if match:
            channel_id, msg_id = match.groups()
            return int(f"-100{channel_id}"), int(msg_id)

        # ----- PUBLIC CHANNEL LINK -----
        match = re.match(PUBLIC_LINK_PATTERN, link)
        if match:
            username, msg_id = match.groups()
            if client is None:
                raise ValueError("Client instance required to resolve username links.")

            chat_id = _resolve_public_channel_id(client, username)
            if chat_id is None:
                raise ValueError(f"Cannot resolve public channel username: {username}")

            return chat_id, int(msg_id)

        # ----- JOINCHAT LINK NOT SUPPORTED -----
        if re.match(JOINCHAT_PATTERN, link):
            raise ValueError(
                "Joinchat invite links do not contain message IDs. "
                "Use a normal post link instead."
            )

        # ----- INVALID LINK -----
        raise ValueError(f"Invalid Telegram link format: {link}")

    # -----------------------------------------------------------
    # Process 1 or 2 links
    # -----------------------------------------------------------
    if len(links) == 1:
        chat_id, last_msg_id = parse_link(links[0])
        return chat_id, 2, last_msg_id

    elif len(links) == 2:
        chat1, start_msg = parse_link(links[0])
        chat2, last_msg = parse_link(links[1])

        if chat1 != chat2:
            raise ValueError("Both links must belong to the same channel.")

        return chat1, start_msg, last_msg

    else:
        raise ValueError("Provide 1 or 2 Telegram links only.")

def clean_series_name(name: str) -> str:
    """Aggressively cleans the series name by removing common tags and metadata."""
    name = re.sub(r"[. _]+", " ", name).strip()
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    common_terms = [
        "4K", "1080P", "720P", "HDRIP", "WEB-DL", "WEBRIP", "BLURAY", "HDTV",
        "AMZN", "NETFLIX", "HULU", "DDP5.1", "X265", "x264", "10BIT", "HEVC",
        "MKV", "AAC", "PSA", "REPACK", "COMPLETE", "WEB",
        r"(19|20)\d{2}",
        "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08", "S09", "S10",
    ]

    for term in common_terms:
        flags = re.IGNORECASE
        if r"(19|20)\d{2}" not in term:
            term = r"\b" + term + r"\b"
        name = re.sub(term, " ", name, flags=flags).strip()

    name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", name).strip()

    channel_tags = ["CK", "UB", "TG", "MOVIE HUB", "FILM"]
    for tag in channel_tags:
        tag_regex = rf"(?:^|[\W_]+){re.escape(tag)}(?:$|[\W_]+)"
        name = re.sub(tag_regex, " ", name, flags=re.IGNORECASE).strip()

    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(
        r'^(?:collection|pack|set|series|season|episode|ep|vol|volume|batch|complete|full|合集|koleksi|release|mux|rip|hq|ultra|theatre|theater|bluray|remux|webdl|webrip|web|netflix|prime|amzn|hulu|hotstar|disney|hbo|tgx|yts|subs|dub|audio|team|group)\b',
        ' ',
        name,
        flags=re.IGNORECASE
    )

    return name.title()


def extract_quality(file_name: str) -> str:
    """Extracts common quality tags from a filename."""
    name = (file_name or "").upper()
    if "2160P" in name or "4K" in name:
        return "4K"
    if "1080P" in name:
        return "1080P"
    if "720P" in name:
        return "720P"
    if "480P" in name:
        return "480P"

    return "480P"


def extract_language(file_name: str) -> str:
    """
    Detects single or multi-audio languages robustly.
    - Works even if 'DUAL'/'MULTI' is missing (e.g., 'ENGLISH+HINDI', 'Tamil & Malayalam').
    - Canonicalizes multi-audio as alphabetically sorted joined with '-'
      e.g., 'Hindi-English', 'Malayalam-Tamil'.
    """
    name = (file_name or "").upper()

    # Normalize separators: treat +, &, /, x, commas and dots/underscores as separators
    sep_normalized = re.sub(r"[+&/x,.\-_]+", " ", name)

    # Map of regex patterns -> canonical language name
    lang_patterns = [
        (r"\b(HINDI|HIN|HI|DUB)\b", "Hindi"),
        (r"\b(ENGLISH|ENG|EN)\b", "English"),
        (r"\b(TAMIL|TAM)\b", "Tamil"),
        (r"\b(TELUGU|TEL)\b", "Telugu"),
        (r"\b(MALAYALAM|MAL)\b", "Malayalam"),
    ]

    found = set()
    for pat, canon in lang_patterns:
        if re.search(pat, sep_normalized):
            found.add(canon)

    # If 'DUAL'/'MULTI' flags exist, still rely on actual matches if we have them.
    if ("DUAL" in name or "MULTI" in name) and not found:
        # Fallback label if they wrote MULTI but didn't name languages.
        return "Multi Audio"

    if not found:
        # Final fallback if nothing matched; your previous default was English.
        return "English"

    if len(found) == 1:
        return next(iter(found))

    # Multi-audio: canonicalize order to avoid 'Hindi-English' vs 'English-Hindi' duplicates
    return "-".join(sorted(found))

def parse_series_info(file_name: str):
    """
    Extracts Series Name, Season (SXX), Episode (EXX), Quality, AND Language.

    Key change:
    - When SxxExx is found, we take ONLY the text to the *left* of that token
      as the series name, so episode titles on the right don't pollute normalized_name.
    """
    if not file_name:
        return "", 0, 0, "", ""

    match = re.search(r"[sS](\d+)[._\-\s]*[eE](\d+)", file_name)
    quality = extract_quality(file_name)
    language = extract_language(file_name)

    if not match:
        # no SxxExx -> fall back to aggressive cleaner
        return clean_series_name(file_name), 0, 0, quality, language

    season = int(match.group(1))
    episode = int(match.group(2))

    # ✅ TAKE ONLY THE LEFT SIDE AS THE SERIES NAME
    left_side = file_name[:match.start()]
    normalized_name = clean_series_name(left_side)
    if not normalized_name:
        # in case left side is empty (rare), fall back to cleaning whole name
        normalized_name = clean_series_name(file_name)

    # keep it reasonably short
    parts = normalized_name.split()
    if len(parts) > 5:
        normalized_name = " ".join(parts[:5])

    return normalized_name.title(), season, episode, quality, language

async def save_series_file_for_bulk_index(message: types.Message):
    file_name = ""

    tg_file_id = None  # NEW

    if message.media:
        if message.media == enums.MessageMediaType.DOCUMENT and message.document:
            file_name = message.document.file_name or ""
            tg_file_id = message.document.file_id  # NEW
        elif message.media == enums.MessageMediaType.VIDEO and message.video:
            file_name = message.video.file_name or ""
            tg_file_id = message.video.file_id  # NEW

    if not file_name and message.caption:
        file_name = message.caption.split('\n')[0].strip()

    if not file_name:
        return False, 0

    normalized_name, season, episode, quality, language = parse_series_info(file_name)

    # skip movies only when no season AND no episode
    if season == 0 and episode == 0:
        logger.info(f"[SKIP MOVIE] {file_name}")
        return False, 0

    # ✅ allow only 1 file per episode
    existing = await get_all_files_by_season_and_quality(normalized_name, season, quality, language)
    for row in existing:
        if row["episode"] == episode:
            logger.info(f"[SKIP] Duplicate ignored → {normalized_name} | S{season}E{episode} | {quality} | {language}")
            return False, 0

    # Fetch OMDb poster (falls back to DEFAULT_POSTER if not found)
    try:
        poster_url = await get_series_poster_async(normalized_name)
        # ensure not None/empty
        poster_url = poster_url or DEFAULT_POSTER
    except Exception as e:
        logger.warning(f"[OMDB ERROR] {normalized_name} -> {e}")
        poster_url = DEFAULT_POSTER

    file_doc = {
        "channel_id": message.chat.id,
        "message_id": message.id,
        "file_name": file_name,
        "normalized_name": normalized_name,
        "season": season,
        "episode": episode,
        "quality": quality,
        "language": language,
        "file_id": tg_file_id,   # NEW
        "poster": poster_url,    # <-- added poster field
        "timestamp": message.date
    }

    # Try saving to DB; if DB rejects unknown keys, fall back to shelve cache for poster only.
    try:
        saved = await save_file_to_db(file_doc)
        if saved:
            return True, 1
        else:
            return False, 0
    except Exception as e:
        logger.warning(f"[DB SAVE FAILED] saving file_doc for {normalized_name} failed: {e}")
        # Save bare minimum metadata to DB (without poster) if your DB expects strict keys
        minimal_doc = {
            "channel_id": message.chat.id,
            "message_id": message.id,
            "file_name": file_name,
            "normalized_name": normalized_name,
            "season": season,
            "episode": episode,
            "quality": quality,
            "language": language,
            "file_id": tg_file_id,
            "timestamp": message.date
        }
        try:
            saved = await save_file_to_db(minimal_doc)
        except Exception as e2:
            logger.error(f"[DB SAVE FAILED - minimal_doc] {e2}")
            saved = False

        # Store poster separately in a shelve cache keyed by normalized_name
        try:
            cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
            with shelve.open(cache_file) as db:
                key = f"poster::{normalized_name.strip().lower()}"
                db[key] = poster_url or DEFAULT_POSTER
        except Exception as e3:
            logger.warning(f"[SHELVE SAVE FAIL] could not cache poster for {normalized_name}: {e3}")

        return (True, 1) if saved else (False, 0)

async def delete_message_later(cyber: Client, chat_id, message_id, delay):
    """Deletes a message after a specified delay."""
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    try:
        await cyber.delete_messages(chat_id, message_id)
    except Exception:
        pass

async def _normalize_channel_id(val):
    """Convert channel id setting to int if possible."""
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return val

async def check_force_subscribe(cyber: Client, user_id: int):
    """Checks if the user is a member of the required force subscribe channel."""
    channel_id_raw = await get_global_setting("FORCE_SUB_CHANNEL_ID", None)
    if not channel_id_raw:
        return True, None  # Force sub is disabled

    channel_id = await _normalize_channel_id(channel_id_raw)

    try:
        member = await cyber.get_chat_member(channel_id, user_id)
        if member.status in [
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]:
            return True, None
        else:
            chat = await cyber.get_chat(channel_id)
            invite_link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else "N/A")
            return False, invite_link

    except Exception as e:
        logger.error(f"Force Sub check failed for channel {channel_id_raw} and user {user_id}: {e}")
        try:
            chat = await cyber.get_chat(channel_id)
            invite_link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else "N/A")
            return False, invite_link
        except Exception:
            return False, "https://t.me/telegram"  
