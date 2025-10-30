import re
from pyrogram import types
from database import save_file_to_db

###################################################
## UTILITY & PARSING (Name Unification Logic) ##
###################################################

def parse_series_info(file_name: str):
    """
    Attempts to extract Series Name, Season (SXX), and Episode (EXX) from a filename.
    Includes enhanced cleaning logic to ensure a pure series name.
    """
    if not file_name:
        return "", 0, 0
        
    # Match SXXEXX patterns
    match = re.search(r"[sS](\d+)[. _-]*[eE](\d+)", file_name)
    
    # If no SXXEXX, we assume it's not a standard series file
    if not match:
        return file_name.title().strip(), 0, 0 

    season = int(match.group(1))
    episode = int(match.group(2))
    
    # 1. Remove SXXEXX pattern first
    normalized_name = re.sub(r"([. _-]+)?[sS]\d+[. _-]*[eE]\d+([. _-]+)?", " ", file_name).strip()
    
    # 2. Clean common garbage terms, quality, language, AND YEAR tags.
    common_terms = [
        "4K", "1080P", "720P", "HDRIP", "WEB-DL", "WEBRIP", "BLURAY", "HDTV", 
        "AMZN", "NETFLIX", "HULU", "DDP5.1", "X265", "x264", "10BIT", "MKV", 
        "MULTISUB", "AAC", "PSA", "DUB", "SUB", "MULTI", "REPACK", "COMPLETE", 
        "ENG", "SPA", "ITA", "FRE", "GER", "DUAL", "AUDIO", 
        r"(19|20)\d{2}",  # Year tag
        "S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08", "S09", "S10"  
    ]
    
    for term in common_terms:
        flags = re.IGNORECASE
        if r"(19|20)\d{2}" not in term:
            term = r"\b" + term + r"\b"
        
        normalized_name = re.sub(term, " ", normalized_name, flags=flags).strip()


    # 3. Aggressively remove bracketed/parenthesized tags AND @channel tags
    normalized_name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", normalized_name).strip()
    
    # 4. ENHANCED CLEANING: Remove common channel/group tags
    channel_tags = ["CK", "UB", "TG", "MOVIE HUB", "FILM"] 
    for tag in channel_tags:
        tag_regex = rf"^\s*{re.escape(tag)}[\s._-]*|[\s._-]*{re.escape(tag)}\s*$"
        normalized_name = re.sub(tag_regex, " ", normalized_name, flags=re.IGNORECASE).strip()
        
    # 5. Final cleaning: collapse multiple separators
    normalized_name = re.sub(r"[. _]+", " ", normalized_name).strip()
    
    # --- NEW LOGIC FOR SUBTITLE UNIFICATION (e.g., The Witcher) ---
    temp_name = normalized_name.lower()
    
    words = normalized_name.split()
    
    if len(words) > 1:
        # Check for very common series names and aggressively unify them
        if temp_name.startswith('the witcher'):
            normalized_name = "The Witcher"
        elif temp_name.startswith('loki'):
            normalized_name = "Loki"
        # Add more common series checks here if needed
        
        # If the name is still long (likely an episode title), truncate at a likely separator
        elif len(words) > 4 and (' ' in normalized_name or '-' in normalized_name):
             # Try to keep only the first few words as the actual series name
             normalized_name = " ".join(words[:4]) 
             
    # --- END NEW LOGIC ---
    
    # 6. LAST STEP: Truncate after the first strong separator (like a stray hyphen)
    if ' - ' in normalized_name:
        normalized_name = normalized_name.split(' - ')[0].strip()
    
    # 7. Final Title Casing
    normalized_name = normalized_name.title().strip()
    
    return normalized_name, season, episode
    

async def save_series_file_for_bulk_index(message: types.Message):
    """
    Helper function to integrate series parsing/saving into the bulk indexer.
    Returns (True if new file saved, status_code)
    status_code: 1=new, 0=duplicate/skipped (non-series), 2=error.
    """
    file_name = ""
    if message.document:
        file_name = message.document.file_name or ""
    elif message.video:
        file_name = message.video.file_name or ""
    else: 
        file_name = getattr(message.audio, "file_name", "")

    normalized_name, season, episode = parse_series_info(file_name)
    
    if season > 0:
        file_doc = {
            "channel_id": message.chat.id,
            "message_id": message.id,
            "file_name": file_name,
            "normalized_name": normalized_name,
            "season": season,
            "episode": episode,
            "timestamp": message.date
        }
        
        # This calls the MongoDB upsert function
        saved = await save_file_to_db(file_doc)
        
        if saved:
            return True, 1 # New file indexed
        else:
            # False from save_file_to_db means duplicate/no change
            return False, 0 
    else:
        # File is media, but not a series (no SXXEXX pattern)
        return False, 0
