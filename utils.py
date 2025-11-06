# utils.py

import re
from pyrogram import types, enums
# Assumes database.py has been imported and save_file_to_db is available
from database import save_file_to_db 

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
    
    return "UNKNOWN"


def extract_language(file_name: str) -> str:
    name = file_name.upper()

    # dual / multi audio (most common groups)
    if "DUAL" in name or "MULTI" in name:
        langs = []

        if "HIN" in name or "HINDI" in name: langs.append("Hindi")
        if "ENG" in name or "ENGLISH" in name: langs.append("English")
        if "TEL" in name or "TELUGU" in name: langs.append("Telugu")
        if "TAM" in name or "TAMIL" in name: langs.append("Tamil")
        if "MAL" in name or "MALAYALAM" in name: langs.append("Malayalam")

        # example: Hindi-English
        if len(langs) >= 2:
            return "-".join(langs)

        return "Multi Audio"

    # single audio detection
    if "HINDI" in name or "HIN" in name or "DUB" in name:
        return "Hindi"

    if "ENGLISH" in name or "ENG" in name:
        return "English"

    if "TAMIL" in name or "TAM" in name:
        return "Tamil"

    if "TELUGU" in name or "TEL" in name:
        return "Telugu"

    if "MALAYALAM" in name or "MAL" in name:
        return "Malayalam"

    return "Unknown"

def parse_series_info(file_name: str):
    """
    Extracts Series Name, Season (SXX), Episode (EXX), Quality, AND Language.
    """
    if not file_name:
        return "", 0, 0, "", ""
        
    match = re.search(r"[sS](\d+)[. _-]*[eE](\d+)", file_name)
    quality = extract_quality(file_name) 
    language = extract_language(file_name) 
    
    if not match:
        return clean_series_name(file_name), 0, 0, quality, language

    season = int(match.group(1))
    episode = int(match.group(2))
    
    name_part = re.sub(r"([. _-]+)?[sS]\d+[. _-]*[eE]\d+([. _-]+)?", " ", file_name).strip()
    normalized_name = clean_series_name(name_part)
    
    if len(normalized_name.split()) > 5:
        normalized_name = " ".join(normalized_name.split()[:5]) 

    return normalized_name.title(), season, episode, quality, language


async def save_series_file_for_bulk_index(message: types.Message):
    """Helper function to integrate series parsing/saving into the bulk indexer."""
    file_name = ""
    
    if message.media:
        if message.media == enums.MessageMediaType.DOCUMENT and message.document:
            file_name = message.document.file_name or ""
        elif message.media == enums.MessageMediaType.VIDEO and message.video:
            file_name = message.video.file_name or ""
    
    if not file_name and message.caption:
        file_name = message.caption.split('\n')[0].strip()

    if not file_name:
        return False, 0 

    normalized_name, season, episode, quality, language = parse_series_info(file_name) 
    
    if season > 0:
        file_doc = {
            "channel_id": message.chat.id,
            "message_id": message.id,
            "file_name": file_name, 
            "normalized_name": normalized_name, 
            "season": season,
            "episode": episode,
            "quality": quality, 
            "language": language, 
            "timestamp": message.date
        }
        
        saved = await save_file_to_db(file_doc)
        
        if saved:
            return True, 1
        else:
            return False, 0
    else:
        return False, 0
