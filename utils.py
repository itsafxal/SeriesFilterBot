import re
from typing import Optional, Tuple

# Common series patterns (s01e01, s01 e01, 1x01, etc.)
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
    
    name_part = filename[:match.start()]
    name_part = re.sub(r'[\s\.\-\[\]_]+$', '', name_part).strip() 
    name = name_part.replace('.', ' ').replace('_', ' ').replace('-', ' ').strip()
    name = re.sub(r'\s+', ' ', name).strip() 

    if not name:
        return None

    season, episode = 0, 0
    
    s_match = re.search(r'[Ss](\d{1,2})', se_part, re.IGNORECASE)
    e_match = re.search(r'[Ee](\d{1,3})', se_part, re.IGNORECASE)
    
    if s_match and e_match:
        season = int(s_match.group(1))
        episode = int(e_match.group(1))
    else:
        x_match = re.search(r'(\d{1,2})x(\d{1,3})', se_part, re.IGNORECASE)
        if x_match:
            season = int(x_match.group(1))
            episode = int(x_match.group(2))

    if season > 0 and episode > 0 and name:
        return name.title(), season, episode
    
    return None

def is_series_file(filename: str) -> bool:
    """Checks if the filename looks like a series episode."""
    return parse_series_info(filename) is not None
