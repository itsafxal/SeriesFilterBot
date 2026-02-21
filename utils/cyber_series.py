import re
from typing import List, Optional

from DB.database import search_series

# -------------------------------------------------
# RAPIDFUZZ (OPTIONAL BUT RECOMMENDED)
# -------------------------------------------------

try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False


# -------------------------------------------------
# QUERY CLEANER
# -------------------------------------------------

def clean_query(q: str) -> str:
    if not q:
        return ""

    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)

    # remove season words
    q = re.sub(r"\bseason\s*\d+\b", "", q)
    q = re.sub(r"\bs\d+\b", "", q)

    q = re.sub(r"\s+", " ", q).strip()
    return q


# -------------------------------------------------
# STRICT RESOLVER (EXACT MATCH ONLY)
# -------------------------------------------------

async def resolve_series(query: str) -> Optional[str]:

    query = clean_query(query)
    if not query:
        return None

    rows = await search_series(query)

    if not rows:
        return None

    query_lower = query.lower()

    for r in rows:
        if r.lower().strip() == query_lower:
            return r

    return None


# -------------------------------------------------
# GET ALL SERIES TITLES (FOR FUZZY SEARCH)
# -------------------------------------------------

async def _get_all_series_titles() -> List[str]:
    """
    Fetch all distinct series names from database.
    This is used for real fuzzy matching when LIKE fails.
    """
    rows = await search_series("")  # empty query returns all
    if not rows:
        return []
    return list(set(rows))


# -------------------------------------------------
# FUZZY SUGGESTIONS (WORKS EVEN IF NO LIKE MATCH)
# -------------------------------------------------

async def suggest_series(query: str, limit: int = 5) -> List[str]:

    query = clean_query(query)

    if not query:
        return []

    # First attempt normal LIKE search
    rows = await search_series(query)

    if rows:
        candidates = list(set(rows))
    else:
        # If LIKE fails → get all titles for fuzzy comparison
        candidates = await _get_all_series_titles()

    if not candidates:
        return []

    # ---------------- RAPIDFUZZ MODE ----------------
    if HAVE_RAPIDFUZZ:
        matches = rf_process.extract(
            query,
            candidates,
            limit=limit,
            scorer=rf_fuzz.ratio,
            score_cutoff=40
        )
        return [m[0] for m in matches]

    # ---------------- FALLBACK SCORING ----------------
    scored = []

    for c in candidates:
        cl = c.lower()

        # Simple similarity scoring
        match_chars = sum(ch in cl for ch in query)
        score = (match_chars / (len(query) + len(cl))) * 200

        # Boost if startswith
        if cl.startswith(query):
            score += 20

        scored.append((c, score))

    scored.sort(key=lambda x: x[1], reverse=True)

    return [x[0] for x in scored[:limit]]


# -------------------------------------------------
# PARSER FOR INDEXING
# -------------------------------------------------

def parse_series_info(file_name: str):
    """
    Parser used during bulk index.
    Returns:
        normalized_name, season, episode, quality, language
    """

    if not file_name:
        return "", 0, 0, "UNKNOWN", "Orginal Language"

    name = file_name

    # ---------------- SEASON / EPISODE ----------------

    season = 0
    episode = 0

    m = re.search(r"[sS](\d+)[^\d]?[eE](\d+)", name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))

    # ---------------- QUALITY ----------------

    quality = "UNKNOWN"
    for q in ["4K", "1080P", "720P", "480P", "360P"]:
        if q.lower() in name.lower():
            quality = q
            break

    # ---------------- LANGUAGE ----------------

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

    language = "-".join(langs) if langs else "Orginal Language"

    # ---------------- NORMALIZE TITLE ----------------

    clean = re.sub(r"[._]", " ", name)
    clean = re.sub(r"\[.*?\]|\(.*?\)", "", clean)
    clean = re.sub(r"[sS]\d+[^\d]?[eE]\d+.*", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip().title()

    return clean, season, episode, quality, language


# -------------------------------------------------
# BUTTON OWNER CHECK
# -------------------------------------------------

async def check_owner(q, uid: int):
    """
    Allow only original requester to use inline buttons.
    """

    if q.from_user.id != int(uid):

        name = q.from_user.first_name or "User"

        await q.answer(
            f"Hey {name}, this is not for you 🙂",
            show_alert=True
        )
        return False

    return True
