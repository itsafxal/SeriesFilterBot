# utils.py

import re

# Common series patterns (s01e01, 1x01, etc.)
SERIES_PATTERN = re.compile(r'([Ss]?\d{1,2}[Ee]\d{1,3}|\d{1,2}x\d{1,3})', re.IGNORECASE)

# Keywords often indicating a movie or non-series file
MOVIE_KEYWORDS = ['movie', '1080p', '720p', 'bluray', 'dvdrip', 'webrip', 'x265', 'film']

def parse_series_info(filename):
    """
    Attempts to extract series name, season, and episode from a filename.
    
    Returns: (series_name, season, episode) or None
    """
    match = SERIES_PATTERN.search(filename)
    if not match:
        return None

    se_part = match.group(0) 
    
    name_part = filename[:match.start()].replace('.', ' ').replace('_', ' ').strip()
    
    name = re.sub(r'\s+', ' ', name_part).strip()

    s_match = re.search(r'[Ss](\d{1,2})', se_part, re.IGNORECASE)
    e_match = re.search(r'[Ee](\d{1,3})', se_part, re.IGNORECASE)
    
    season = int(s_match.group(1)) if s_match else 0
    episode = int(e_match.group(1)) if e_match else 0

    return name.title(), season, episode

def is_series_file(filename):
    """Checks if the filename looks like a series episode and not a movie."""
    # Must have a Season/Episode pattern
    if not SERIES_PATTERN.search(filename):
        return False

    # If it contains S/E, it's highly likely a series, regardless of movie keywords.
    return True
