# utils/series_helper.py

import re
from typing import List, Optional

from DB.database import search_series

try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False


# -------------------------------------------------
# BASIC QUERY CLEAN
# -------------------------------------------------

def clean_query(q: str) -> str:
    if not q:
        return ""
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


# -------------------------------------------------
# MAIN RESOLVER (USED BY FILTERS)
# -------------------------------------------------

async def resolve_series(query: str):
    """
    STRICT resolver:
    - exact match
    - startswith strong match
    - fuzzy only for strong similarity
    """

    query = clean_query(query)
    if not query:
        return None

    # remove season words (S01, season 1, etc)
    query = re.sub(r"\bseason\s*\d+\b", "", query)
    query = re.sub(r"\bs\d+\b", "", query)
    query = query.strip()

    rows = await search_series(query)
    if not rows:
        return None

    titles = list(set(rows))
    ql = query.lower()

    # ---------- EXACT MATCH ----------
    for t in titles:
        if t.lower() == ql:
            return t

    # ---------- STARTSWITH MATCH ----------
    for t in titles:
        if t.lower().startswith(ql):
            # prevent very short accidental matches
            if len(ql) >= 5:
                return t

    # ---------- STRICT FUZZY ----------
    if HAVE_RAPIDFUZZ:
        match = rf_process.extractOne(
            query,
            titles,
            scorer=rf_fuzz.ratio,   # stricter than partial_ratio
            score_cutoff=80         # HIGH threshold
        )
        if match:
            return match[0]

    # ❌ NO CONTAINS FALLBACK
    return None



# -------------------------------------------------
# SUGGESTIONS FOR WRONG INPUT
# -------------------------------------------------

async def suggest_series(query: str, limit: int = 5) -> List[str]:
    query = clean_query(query)
    if not query or not HAVE_RAPIDFUZZ:
        return []

    rows = await search_series("")
    if not rows:
        return []

    choices = list(set(rows))

    matches = rf_process.extract(
        query,
        choices,
        limit=limit,
        score_cutoff=40
    )

    return [m[0] for m in matches]


# -------------------------------------------------
# PARSER FOR INDEXING (USED BY index_helper.py)
# -------------------------------------------------

def parse_series_info(file_name: str):
    """
    Parser used during bulk index.
    Returns: normalized_name, season, episode, quality, language
    """

    if not file_name:
        return "", 0, 0, "UNKNOWN", "English"

    name = file_name

    # Season / Episode
    season = 0
    episode = 0

    m = re.search(r"[sS](\d+)[^\d]?[eE](\d+)", name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))

    # Quality
    quality = "UNKNOWN"
    for q in ["4K", "1080P", "720P", "480P", "360P"]:
        if q.lower() in name.lower():
            quality = q
            break

    # Language detection
    langs = []
    upper = name.upper()

    if "HINDI" in upper:
        langs.append("Hindi")
    if "ENGLISH" in upper:
        langs.append("English")
    if "TAMIL" in upper:
        langs.append("Tamil")
    if "TELUGU" in upper:
        langs.append("Telugu")
    if "MALAYALAM" in upper:
        langs.append("Malayalam")

    language = "-".join(langs) if langs else "English"

    # Normalize series title
    clean = re.sub(r"[._]", " ", name)
    clean = re.sub(r"\[.*?\]|\(.*?\)", "", clean)
    clean = re.sub(r"[sS]\d+[^\d]?[eE]\d+.*", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip().title()

    return clean, season, episode, quality, language
