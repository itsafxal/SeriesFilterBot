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
# MAIN RESOLVER
# -------------------------------------------------

async def resolve_series(query: str) -> Optional[str]:
    """
    Try to resolve user input into a known normalized series name.
    Returns best match or None.
    """
    query = clean_query(query)
    if not query:
        return None

    # SQLite search already gives reasonable candidates
    rows = await search_series(query)
    if not rows:
        return None

    titles = list(set(rows))

    # Exact or startswith preference
    ql = query.lower()
    for t in titles:
        if t.lower().startswith(ql):
            return t

    # RapidFuzz ranking
    if HAVE_RAPIDFUZZ:
        match = rf_process.extractOne(
            query,
            titles,
            scorer=rf_fuzz.partial_ratio,
            score_cutoff=60
        )
        if match:
            return match[0]

    # Fallback partial contain
    for t in titles:
        if ql in t.lower():
            return t

    return titles[0]


# -------------------------------------------------
# SUGGESTIONS (when resolve fails)
# -------------------------------------------------

async def suggest_series(query: str, limit: int = 5) -> List[str]:
    """
    Return close series name suggestions when no direct match found.
    Example: 'wed' -> ['Wednesday', 'Wedland', ...]
    """
    query = clean_query(query)
    if not query or not HAVE_RAPIDFUZZ:
        return []

    # pull broad candidate set from sqlite
    rows = await search_series("")  # empty query returns all names
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
