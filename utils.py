import re
from typing import Optional, Tuple

# Common series patterns (s01e01, s01 e01, 1x01, etc.)
# Allows SXX EYY, SXX.EYY, SXX-EYY, or SXXEYY, or 1x01 format
SERIES_PATTERN = re.compile(
    r'([Ss]?\d{1,2}[\.\- ]?[Ee]\d{1,3}|\d{1,2}x\d{1,3})', 
    re.IGNORECASE
)

def parse_series_info(filename: str) -> Optional[Tuple[str, int, int]]:
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
    name_part = re.sub(r'[\s\.\-\[\]_]+$', '', name_part).strip() 
    
    # 3. Replace internal separators ('.', '_', '-') with a single space for a clean, searchable name
    name = name_part.replace('.', ' ').replace('_', ' ').replace('-', ' ').strip()
    name = re.sub(r'\s+', ' ', name).strip() # Reduce multiple spaces to one

    # If the name is empty after cleanup, reject it
    if not name:
        return None

    season, episode = 0, 0
    
    # 4. S/E parsing (S01E01 format)
    s_match = re.search(r'[Ss](\d{1,2})', se_part, re.IGNORECASE)
    e_match = re.search(r'[Ee](\d{1,3})', se_part, re.IGNORECASE)
    
    if s_match and e_match:
        season = int(s_match.group(1))
        episode = int(e_match.group(1))
    else:
        # Try 1x01 format
        x_match = re.search(r'(\d{1,2})x(\d{1,3})', se_part, re.IGNORECASE)
        if x_match:
            season = int(x_match.group(1))
            episode = int(x_match.group(2))

    # If both season and episode are found, and a name exists, it's a success
    if season > 0 and episode > 0 and name:
        # Convert to Title Case for database consistency
        return name.title(), season, episode
    
    return None

def is_series_file(filename: str) -> bool:
    """Checks if the filename looks like a series episode."""
    return parse_series_info(filename) is not None
