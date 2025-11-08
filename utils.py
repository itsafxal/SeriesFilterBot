# utils.py

import re
from pyrogram import types, enums
from config import logger
from database import save_file_to_db, get_all_files_by_season_and_quality

###################################################
## UTILITY & PARSING (Name Unification Logic) ##
###################################################

# Define common language terms and their full names for multi-audio parsing
LANGUAGE_CODES = {
    "HIN": "Hindi",
    "ENG": "English",
    "TAM": "Tamil",
    "TEL": "Telugu",
    "MALA": "Malayalam",
    "DUB": "Hindi" # Often used alone to indicate Hindi dub
}
# Create a pattern string for the regex to match any of the known codes
LANG_CODE_PATTERN = "|".join(LANGUAGE_CODES.keys())


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
    name = file_name.upper()
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
    
    if season <= 0:
        logger.info(f"[SKIP: MOVIE] {file_name}")
        return False, 0
    

    # ✅ allow only 1 file per episode
    existing = await get_all_files_by_season_and_quality(normalized_name, season, quality, language)
    for row in existing:
        if row["episode"] == episode:
            logger.info(f"[SKIP] Duplicate ignored → {normalized_name} | S{season}E{episode} | {quality} | {language}")
            return False, 0

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
        "timestamp": message.date
    }

    saved = await save_file_to_db(file_doc)
    return (True, 1) if saved else (False, 0)
