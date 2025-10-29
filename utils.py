# utils.py

import re
# ... (rest of imports and patterns)

def parse_series_info(filename):
    """
    Attempts to extract series name, season, and episode from a filename.
    
    Returns: (series_name, season, episode) or None
    """
    match = SERIES_PATTERN.search(filename)
    if not match:
        return None

    se_part = match.group(0) 
    
    # 1. Get the name part
    name_part = filename[:match.start()]
    
    # 2. Aggressively strip trailing non-word characters and separators (FIX)
    # This removes characters like ' [', ' .', ' -', ']' from the end of the name
    name_part = re.sub(r'[\s\.\-\[\]]+$', '', name_part).strip() # 👈 NEW CLEANUP STEP
    
    # 3. Replace internal separators and clean up spaces
    name = name_part.replace('.', ' ').replace('_', ' ').strip()
    name = re.sub(r'\s+', ' ', name).strip()

    # If the name is empty after cleanup, try to use the part before the first separator
    if not name and name_part:
         name = name_part.split(' ')[0]

    # ... (rest of S/E parsing remains the same)
    s_match = re.search(r'[Ss](\d{1,2})', se_part, re.IGNORECASE)
    e_match = re.search(r'[Ee](\d{1,3})', se_part, re.IGNORECASE)
    
    season = int(s_match.group(1)) if s_match else 0
    episode = int(e_match.group(1)) if e_match else 0
    
    # If both season and episode are found, it's a success
    if season > 0 and episode > 0 and name:
        return name.title(), season, episode
    
    return None
