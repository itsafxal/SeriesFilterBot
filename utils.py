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
    """
    Extracts language tags from a filename, with enhanced multi-language parsing.
    """
    name = file_name.upper()
    
    # --- 1. Enhanced Multi-Language Detection ---
    if re.search(r"\bDUAL\b|\bMULTI\b", name):
        
        # Pattern to capture two distinct language codes near each other
        # e.g., HIN.ENG, [TAMIL-TELUGU], MALA-ENG, etc.
        pattern = rf"(?:{LANG_CODE_PATTERN})[._-]*({LANG_CODE_PATTERN})[._-]*({LANG_CODE_PATTERN})"
        
        match = re.search(pattern, name)
        
        if match:
            # Group 1 is the first code, Group 2 is the second code
            code1 = match.group(1).upper()
            code2 = match.group(2).upper()
            
            lang1 = LANGUAGE_CODES.get(code1, code1)
            lang2 = LANGUAGE_CODES.get(code2, code2)
            
            # Combine if they are different and meaningful
            if lang1 != lang2:
                # Format: Hindi-English
                return f"{lang1}-{lang2}" 

        # Fallback for general dual/multi tags when specific languages can't be paired
        if "HINDI" in name and "ENGLISH" in name:
            return "Dual Audio (Hindi-English)"
        
        # General fallback
        return "Dual/Multi Audio"

    # --- 2. Single Language Detection ---
    
    # Priority for Indian languages (since they are often the DUB)
    if any(tag in name for tag in ["HINDI", "HIN", "DUB"]):
        return "Hindi"
    
    if any(tag in name for tag in ["MALAYALAM", "MALA"]):
        return "Malayalam"
        
    if any(tag in name for tag in ["TAMIL", "TAM", "TELUGU", "TEL"]):
        return "Indian Regional"
    
    # English/Subtitles last
    if any(tag in name for tag in ["ENGLISH", "ENG", "SUB"]):
        return "English"
        
    return "Unknown" # Cleaned default language


def parse_series_info(file_name: str):
    """
    Extracts Series Name, Season (SXX), Episode (EXX), Quality, AND Language.
    **MODIFIED: Returns original name part instead of normalized name.**
    """
    if not file_name:
        return "", 0, 0, "", ""
        
    match = re.search(r"[sS](\d+)[. _-]*[eE](\d+)", file_name)
    quality = extract_quality(file_name) 
    language = extract_language(file_name) 
    
    if not match:
        # Use the whole file_name as the series identifier if no SXXEXX pattern is found
        # This will be stored in the 'file_name_for_search' field in the DB.
        return file_name.strip(), 0, 0, quality, language 

    season = int(match.group(1))
    episode = int(match.group(2))
    
    # Remove SXXEXX portion to get the name part
    name_part = re.sub(r"([. _-]+)?[sS]\d+[. _-]*[eE]\d+([. _-]+)?", " ", file_name).strip()
    
    # Use a simpler name portion, but NO aggressive cleaning
    # We will use this as the primary search/grouping identifier
    series_name_for_search = name_part 
    
    return series_name_for_search, season, episode, quality, language


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

    # NOTE: The first return value (series_name_for_search) is no longer aggressively cleaned
    series_name_for_search, season, episode, quality, language = parse_series_info(file_name) 
    
    if season > 0:
        file_doc = {
            "channel_id": message.chat.id,
            "message_id": message.id,
            "file_name": file_name, 
            # Storing the un-normalized name for grouping/searching
            "file_name_for_search": series_name_for_search, 
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
