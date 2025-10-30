# utils.py

import re
from pyrogram import types
from database import save_file_to_db # Assuming database is accessible

###################################################
## UTILITY & PARSING (Name Unification Logic) ##
###################################################

def clean_series_name(name: str) -> str:
    """Aggressively cleans the series name by removing quality tags, groups, and subtitles."""
    # 1. Remove common separators and collapse spaces
    name = re.sub(r"[. _]+", " ", name).strip()

    # 2. Clean common garbage terms, quality, language, AND YEAR tags.
    common_terms = [
        "4K", "1080P", "720P", "HDRIP", "WEB-DL", "WEBRIP", "BLURAY", "HDTV", 
        "AMZN", "NETFLIX", "HULU", "DDP5.1", "X265", "x264", "10BIT", "HEVC", 
        "MKV", "AAC", "PSA", "DUB", "SUB", "MULTI", "REPACK", "COMPLETE", "WEB",
        "ENG", "SPA", "ITA", "FRE", "GER", "DUAL", "AUDIO", "2CH",
        r"(19|20)\d{2}",  # Year tag
        "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08", "S09", "S10", 
    ]
    
    for term in common_terms:
        flags = re.IGNORECASE
        if r"(19|20)\d{2}" not in term:
            term = r"\b" + term + r"\b"
        
        name = re.sub(term, " ", name, flags=flags).strip()

    # 3. Aggressively remove bracketed/parenthesized tags AND @channel tags
    name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", name).strip()
    
    # 4. Remove common channel/group tags
    channel_tags = ["CK", "UB", "TG", "MOVIE HUB", "FILM"] 
    for tag in channel_tags:
        tag_regex = rf"^\s*{re.escape(tag)}[\s._-]*|[\s._-]*{re.escape(tag)}\s*$"
        name = re.sub(tag_regex, " ", name, flags=re.IGNORECASE).strip()
        
    # 5. Collapse multiple spaces again
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
    if "360P" in name:
        return "360P"
        
    return "UNKNOWN"


def parse_series_info(file_name: str):
    """
    Extracts Series Name, Season (SXX), Episode (EXX), AND Quality from a filename.
    """
    if not file_name:
        return "", 0, 0, "" 
        
    match = re.search(r"[sS](\d+)[. _-]*[eE](\d+)", file_name)
    quality = extract_quality(file_name) # <-- New: Extract quality first

    if not match:
        return clean_series_name(file_name), 0, 0, quality 

    season = int(match.group(1))
    episode = int(match.group(2))
    
    # 1. Remove SXXEXX pattern first
    name_part = re.sub(r"([. _-]+)?[sS]\d+[. _-]*[eE]\d+([. _-]+)?", " ", file_name).strip()
    
    # 2. Aggressively clean the remaining text
    normalized_name = clean_series_name(name_part)
    
    # 3. ENHANCED UNIFICATION: Force specific series to a single name (for the search fix)
    temp_name = normalized_name.lower()
    
    if temp_name.startswith('the flash'):
        normalized_name = 'The Flash'
    # ... (Other specific unifications)

    elif len(normalized_name.split()) > 5:
        normalized_name = " ".join(normalized_name.split()[:5]) 

    return normalized_name.title(), season, episode, quality # <-- Return quality
    

async def save_series_file_for_bulk_index(message: types.Message):
    """
    Helper function to integrate series parsing/saving into the bulk indexer.
    """
    file_name = ""
    if message.document:
        file_name = message.document.file_name or ""
    elif message.video:
        file_name = message.video.file_name or ""
    else: 
        file_name = getattr(message.audio, "file_name", "")

    normalized_name, season, episode, quality = parse_series_info(file_name) # <-- Get quality
    
    if season > 0:
        file_doc = {
            "channel_id": message.chat.id,
            "message_id": message.id,
            "file_name": file_name,
            "normalized_name": normalized_name, 
            "season": season,
            "episode": episode,
            "quality": quality, # <-- New: Save quality
            "timestamp": message.date
        }
        
        saved = await save_file_to_db(file_doc)
        
        if saved:
            return True, 1
        else:
            return False, 0
    else:
        return False, 0
