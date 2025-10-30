# utils.py - MODIFIED CODE

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
    
    # Check for SXX format without EXX (sometimes used for full season files or just SXX in the title)
    season_only_match = re.search(r"[sS](\d+)(?![. _-]*[eE])", file_name) 

    if match or season_only_match:
        # Prioritize SXXEXX match for season/episode, otherwise use SXX match
        if match:
             season = int(match.group(1))
             episode = int(match.group(2))
             # Remove SXXEXX pattern first
             temp_name = re.sub(r"([. _-]+)?[sS]\d+[. _-]*[eE]\d+([. _-]+)?", " ", file_name).strip()
        elif season_only_match:
             season = int(season_only_match.group(1))
             episode = 0 # Cannot determine episode
             # Remove SXX pattern
             temp_name = re.sub(r"([. _-]+)?[sS]\d+([. _-]+)?", " ", file_name).strip()
             
        # Use temp_name for initial cleaning
        normalized_name = temp_name
        
        # ... (Rest of your existing cleaning logic for common_terms, brackets, etc. remains the same) ...
        # Ensure the entire block from 'common_terms' to 'Final Title Casing' is applied to normalized_name
        
        # 1. Clean common garbage terms, quality, language, AND YEAR tags.
        common_terms = [
            "4K", "1080P", "720P", "HDRIP", "WEB-DL", "WEBRIP", "BLURAY", "HDTV", 
            "AMZN", "NETFLIX", "HULU", "DDP5.1", "X265", "x264", "10BIT", "MKV", 
            "MULTISUB", "AAC", "PSA", "DUB", "SUB", "MULTI", "REPACK", "COMPLETE", 
            "ENG", "SPA", "ITA", "FRE", "GER", "DUAL", "AUDIO", 
            r"(19|20)\d{2}",  # Year tag
            # Removed S01, S02... from here as they're now handled in the main logic
        ]
        
        for term in common_terms:
            flags = re.IGNORECASE
            if r"(19|20)\d{2}" not in term:
                term = r"\b" + term + r"\b"
            
            normalized_name = re.sub(term, " ", normalized_name, flags=flags).strip()


        # 2. Aggressively remove bracketed/parenthesized tags AND @channel tags
        normalized_name = re.sub(r"\[.*?\]|\(.*?\)|@\S+", " ", normalized_name).strip()
        
        # 3. ENHANCED CLEANING: Remove common channel/group tags
        channel_tags = ["CK", "UB", "TG", "MOVIE HUB", "FILM"] 
        for tag in channel_tags:
            tag_regex = rf"^\s*{re.escape(tag)}[\s._-]*|[\s._-]*{re.escape(tag)}\s*$"
            normalized_name = re.sub(tag_regex, " ", normalized_name, flags=re.IGNORECASE).strip()
            
        # 4. Final cleaning: collapse multiple separators
        normalized_name = re.sub(r"[. _]+", " ", normalized_name).strip()
        
        # 5. NEW LOGIC TO REMOVE EPISODE TITLES/SUBTITLES
        # If the filename contains a series name followed by a subtitle/episode title separated by a hyphen or a colon
        # E.g., The.Witcher.S01E01.A.Sermon.Of.Survival
        # Since we removed SXXEXX, we are left with 'The Witcher A Sermon Of Survival'
        
        # Find the first capital letter that is not at the start of the string, or the first strong separator
        # This is complex, so let's use a simpler heuristic for now: check for very common series names
        
        # Heuristic for single-word series (like Loki or Witcher)
        words = normalized_name.split()
        if words and len(words) > 1:
            # Check if the first word/two words are a common series name and the rest is episode title
            potential_series_name = words[0]
            
            # Simple check for very long names which are likely titles
            if len(words) > 5:
                normalized_name = " ".join(words[:2]) # Take only the first two words
                
            # If the name is just a common series name followed by a long string (e.g., 'The Witcher A Sermon...')
            if normalized_name.lower().startswith('the witcher') and len(words) > 3:
                 normalized_name = "The Witcher"
            elif normalized_name.lower().startswith('loki') and len(words) > 2:
                 normalized_name = "Loki"
            # Add other common series names here (e.g., 'Game Of Thrones', 'Stranger Things')
            # Example:
            # elif normalized_name.lower().startswith('game of thrones') and len(words) > 4:
            #      normalized_name = "Game Of Thrones"


        # 6. LAST STEP: Truncate after the first strong separator (like a stray hyphen)
        if ' - ' in normalized_name:
            normalized_name = normalized_name.split(' - ')[0].strip()
        
        # 7. Final Title Casing
        normalized_name = normalized_name.title().strip()
        
        # Final safety check: if the name is too short, revert to original file_name title
        if len(normalized_name) < 3 and len(file_name) > 5:
             normalized_name = file_name.title().strip()
             
        return normalized_name, season, episode
        
    # If no SXX is found, return original name and 0, 0
    return file_name.title().strip(), 0, 0
