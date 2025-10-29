# utils.py
import re

# Common series patterns (s01e01, s01 e01, 1x01, etc.)
# Allows SXX EYY, SXX.EYY, SXX-EYY, or SXXEYY
SERIES_PATTERN = re.compile(
    r'([Ss]?\d{1,2}[\.\- ]?[Ee]\d{1,3}|\d{1,2}x\d{1,3})', 
    re.IGNORECASE
)

def parse_series_info(filename):
    """
    Attempts to extract series name, season, and episode from a filename.
    Returns: (series_name, season, episode) or None
    """
    match = SERIES_PATTERN.search(filename)
    if not match:
        return None

    se_part = match.group(0) 
    
    # 1. Get the name part (up to the SXXEXX match)
    name_part = filename[:match.start()]
    
    # 2. Aggressively strip trailing separators/delimiters from the end of the name part
    # This removes characters like ' [', ' .', ' -', ']' from the end of the name
    name_part = re.sub(r'[\s\.\-\[\]_]+$', '', name_part).strip() 
    
    # 3. Replace internal separators ('.', '_', '-') with a single space for a clean, searchable name
    name = name_part.replace('.', ' ').replace('_', ' ').replace('-', ' ').strip()
    name = re.sub(r'\s+', ' ', name).strip() # Reduce multiple spaces to one

    # If the name is empty after cleanup, reject it
    if not name:
        return None

    # 4. S/E parsing
    s_match = re.search(r'[Ss](\d{1,2})', se_part, re.IGNORECASE)
    e_match = re.search(r'[Ee](\d{1,3})', se_part, re.IGNORECASE)
    
    season = int(s_match.group(1)) if s_match else 0
    episode = int(e_match.group(1)) if e_match else 0
    
    # If both season and episode are found, and a name exists, it's a success
    if season > 0 and episode > 0 and name:
        # Convert to Title Case for database consistency
        return name.title(), season, episode
    
    return None

def is_series_file(filename):
    """Checks if the filename looks like a series episode."""
    return parse_series_info(filename) is not None
