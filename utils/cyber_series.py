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
import re

def parse_series_info(file_name: str):
    """
    Parser used during bulk index.

    Returns:
        normalized_name, season, episode, quality, language
    """

    if not file_name:
        return "", 0, 0, "UNKNOWN", "Unknown"

    name = file_name
    upper = name.upper()

    # =================================================
    # SEASON / EPISODE DETECTION
    # =================================================

    season = 0
    episode = 0

    # Pattern: S01E02 / S1E2 / s01 e02
    m = re.search(r"[sS](\d+)[^\d]?[eE](\d+)", name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))

    # =================================================
    # QUALITY DETECTION
    # =================================================

    quality = "UNKNOWN"

    for q in ["4K", "1080P", "720P", "480P", "360P"]:
        if q in upper:
            quality = q
            break

    # =================================================
    # LANGUAGE DETECTION (FROM FILENAME ONLY)
    # =================================================

    langs = set()

    # ---------- Full language keywords ----------
    LANG_KEYWORDS = {
        "HINDI": "Hindi",
        "ENGLISH": "English",
        "TAMIL": "Tamil",
        "TELUGU": "Telugu",
        "MALAYALAM": "Malayalam",
        "KANNADA": "Kannada"
    }

    for key, value in LANG_KEYWORDS.items():
        if key in upper:
            langs.add(value)

    # ---------- Short tags ----------
    SHORT_TAGS = {
        r"\bHIN\b": "Hindi",
        r"\bENG\b": "English",
        r"\bTAM\b": "Tamil",
        r"\bTEL\b": "Telugu",
        r"\bMAL\b": "Malayalam",
        r"\bKAN\b": "Kannada"
    }

    for pattern, value in SHORT_TAGS.items():
        if re.search(pattern, upper):
            langs.add(value)

    # ---------- Multi / Dual detection ----------
    if "DUAL" in upper or "MULTI" in upper:
        return _normalize_title(name), season, episode, quality, "Multi Audio"

    # ---------- Final resolution ----------
    if len(langs) == 1:
        language = next(iter(langs))
    elif len(langs) > 1:
        language = "-".join(sorted(langs))
    else:
        language = "Unknown"

    # =================================================
    # TITLE NORMALIZATION
    # =================================================

    normalized_title = _normalize_title(name)

    return normalized_title, season, episode, quality, language


# =================================================
# TITLE NORMALIZER
# =================================================

def _normalize_title(name: str):

    clean = name

    # Replace separators
    clean = re.sub(r"[._\-]", " ", clean)

    # Remove brackets
    clean = re.sub(r"\[.*?\]|\(.*?\)", "", clean)

    # Remove season/episode and everything after
    clean = re.sub(r"[sS]\d+[^\d]?[eE]\d+.*", "", clean)

    # Remove quality + junk tags
    clean = re.sub(
        r"\b(4K|1080P|720P|480P|360P|HDRIP|WEBRIP|WEB|BLURAY|X264|X265|HEVC|AAC|DDP5\.1|REPACK|COMPLETE|BATCH)\b",
        "",
        clean,
        flags=re.IGNORECASE
    )

    # Remove year
    clean = re.sub(r"\b(19|20)\d{2}\b", "", clean)

    # Remove extra spaces
    clean = re.sub(r"\s+", " ", clean).strip()

    return clean.title()

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
