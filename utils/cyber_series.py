"""
cyber_series.py — SMART SUGGESTION ENGINE
==========================================

STRICT RULE:
  A title is suggested ONLY IF:
    1. It exists in our SQL database
    2. TMDB or OMDB also recognizes it (returns a result)

  If EITHER condition fails → title is NOT suggested.

EXAMPLES:
  "lucifer" typed:
    → SQL finds "Lucifer" ✓
    → TMDB finds it, year=2016 ✓
    → Suggest: "Lucifer (2016)" ✓

  "nf lucifer" typed:
    → SQL finds "NF Lucifer" ✓
    → TMDB/OMDB has no match for "NF Lucifer" ✗
    → NOT suggested ✗

  "xyz show" typed:
    → SQL finds nothing ✗
    → NOT suggested ✗ (regardless of TMDB)

  Typo "luciferr" typed:
    → SQL LIKE fails
    → RapidFuzz finds "Lucifer" in DB ✓
    → TMDB confirms "Lucifer" ✓
    → Suggest: "Lucifer (2016)" ✓
"""

import re
import asyncio
import time
from typing import List, Optional, Tuple

from DB.database import search_series, get_all_series

# ─────────────────────────────────────────────────────────────
# RAPIDFUZZ
# ─────────────────────────────────────────────────────────────

try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False


# ─────────────────────────────────────────────────────────────
# SERIES REGEX
# ─────────────────────────────────────────────────────────────

SERIES_RE = re.compile(
    r"(?i)\b("
    r"S\d{1,3}E\d{1,3}"
    r"|Season\s?\d+\s?[-]?\s?Episode\s?\d+"
    r"|\d{1,2}x\d{1,2}"
    r"|E\d{1,3}(-E\d{1,3})?"
    r"|EP\d{1,3}"
    r"|(19|20)\d{2}.*S\d{1,3}E\d{1,3}"
    r"|Combined"
    r"|S\d{1,3}\s?EP\d{1,3}"
    r"|S\d{1,3}E\d{1,3}E\d{1,3}"
    r"|S\d{1,3}"
    r"|Season\s?\d+"
    r")\b"
)


# ─────────────────────────────────────────────────────────────
# TMDB / OMDB LOOKUP
# Returns (year, found) — found=True means API confirmed the title
# Cached 6 hours per title. Semaphore limits to 3 concurrent calls.
# ─────────────────────────────────────────────────────────────

# Cache: title_key → {"year": str|None, "found": bool, "ts": float}
_API_CACHE:      dict[str, dict] = {}
_API_CACHE_TTL = 6 * 3600
_API_SEM        = asyncio.Semaphore(3)


async def _lookup_title(title: str) -> Tuple[Optional[str], bool]:
    """
    Check TMDB then OMDB for the given title.

    Returns:
        (year, found)
        year  — "2016" or None
        found — True if TMDB or OMDB returned a result for this title
                False if both APIs returned nothing (→ don't suggest this title)
    """
    key = title.lower().strip()
    entry = _API_CACHE.get(key)
    if entry and (time.time() - entry["ts"]) < _API_CACHE_TTL:
        return entry["year"], entry["found"]

    year  = None
    found = False

    async with _API_SEM:
        # ── TMDB ─────────────────────────────────────────────
        try:
            import aiohttp
            from config import TMDB_API_KEY
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.themoviedb.org/3/search/tv",
                    params={"api_key": TMDB_API_KEY, "query": title},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    data = await r.json()
            results = data.get("results", [])
            if results:
                found = True
                date  = results[0].get("first_air_date", "") or ""
                year  = date[:4] if len(date) >= 4 else None
        except Exception:
            pass

        # ── OMDB fallback (only if TMDB didn't find it) ──────
        if not found:
            try:
                import aiohttp
                from config import OMDB_API_KEY
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        "http://www.omdbapi.com/",
                        params={"apikey": OMDB_API_KEY, "t": title, "type": "series"},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as r:
                        data = await r.json()
                if data.get("Response") == "True":
                    found    = True
                    year_raw = data.get("Year", "") or ""
                    year     = year_raw[:4] if len(year_raw) >= 4 else None
            except Exception:
                pass

    _API_CACHE[key] = {"year": year, "found": found, "ts": time.time()}
    return year, found


# ─────────────────────────────────────────────────────────────
# QUERY CLEANER
# ─────────────────────────────────────────────────────────────

def clean_query(q: str) -> str:
    if not q:
        return ""
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = SERIES_RE.sub("", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


# ─────────────────────────────────────────────────────────────
# STEP 1 — SQL: find candidates in our DB
# ─────────────────────────────────────────────────────────────

async def _sql_candidates(query: str, limit: int = 8) -> List[str]:
    """Returns titles from DB matching the query via SQL LIKE."""
    results = await search_series(query)
    if results:
        return list(results)[:limit]

    # Try first word fallback (e.g. "breaking" for "breaking bad s01")
    words = query.split()
    if len(words) > 1 and len(words[0]) >= 3:
        results = await search_series(words[0])
        if results:
            return list(results)[:limit]

    return []


# ─────────────────────────────────────────────────────────────
# STEP 2 — FUZZY: typo correction against DB titles only
# ─────────────────────────────────────────────────────────────

async def _fuzzy_candidates(query: str, limit: int = 6) -> List[str]:
    """
    When SQL finds nothing, run RapidFuzz against all DB titles.
    Returns only titles scoring >= 72%.
    NEVER introduces titles not in DB.
    """
    if not HAVE_RAPIDFUZZ:
        return []

    try:
        all_series = await get_all_series()   # [(name, count), ...]
        all_titles = [n for n, _ in all_series] if all_series else []
    except Exception:
        return []

    if not all_titles:
        return []

    matches = rf_process.extract(
        query,
        all_titles,
        scorer=rf_fuzz.token_sort_ratio,
        limit=limit,
        score_cutoff=72,
    )
    matches.sort(key=lambda x: x[1], reverse=True)
    return [m[0] for m in matches]


# ─────────────────────────────────────────────────────────────
# STEP 3 — FILTER: validate each DB candidate against TMDB/OMDB
# Titles not found by TMDB/OMDB are silently dropped.
# ─────────────────────────────────────────────────────────────

async def _validate_and_enrich(
    candidates: List[str],
) -> List[Tuple[str, Optional[str]]]:
    """
    For each DB candidate:
      - Call TMDB/OMDB
      - If found → keep it, attach year (may be None if API has no year)
      - If NOT found → drop it

    All lookups are concurrent (limited by _API_SEM).
    Returns [(title, year_or_None), ...]  — only validated titles.
    """
    lookups = await asyncio.gather(
        *[_lookup_title(t) for t in candidates],
        return_exceptions=True,
    )

    validated = []
    for title, result in zip(candidates, lookups):
        if isinstance(result, Exception):
            # API error → be safe, don't drop (treat as unconfirmed)
            continue
        year, found = result
        if found:                        # ← strict gate: TMDB/OMDB must confirm
            validated.append((title, year))

    return validated


# ─────────────────────────────────────────────────────────────
# PUBLIC: suggest_series_with_year
# Main function — returns validated (title, year) pairs
# ─────────────────────────────────────────────────────────────

async def suggest_series_with_year(
    query: str, limit: int = 4
) -> List[Tuple[str, Optional[str]]]:
    """
    Returns up to `limit` (title, year) tuples where:
      - title is in our SQL database
      - title is confirmed by TMDB or OMDB
      - year is from TMDB/OMDB ("2016"), or None if not in their data

    If TMDB/OMDB doesn't know the title → it's excluded even if in DB.
    """
    q = clean_query(query)
    if not q:
        return []

    # Step 1: SQL candidates
    candidates = await _sql_candidates(q, limit=limit * 3)

    # Step 2: Fuzzy fallback if SQL found nothing
    if not candidates:
        candidates = await _fuzzy_candidates(q, limit=limit * 2)

    if not candidates:
        return []

    # Deduplicate preserving order
    seen, unique = set(), []
    for c in candidates:
        k = c.lower().strip()
        if k not in seen:
            seen.add(k)
            unique.append(c)

    # Step 3: Validate each against TMDB/OMDB — drop unrecognised titles
    validated = await _validate_and_enrich(unique)

    return validated[:limit]


# ─────────────────────────────────────────────────────────────
# PUBLIC: suggest_series (backward-compat, plain title list)
# Used by existing resolve_series_with_suggestions in filters.py
# ─────────────────────────────────────────────────────────────

async def suggest_series(query: str, limit: int = 4) -> List[str]:
    """
    Returns plain title strings (no year).
    Only titles that pass DB + TMDB/OMDB validation.
    """
    results = await suggest_series_with_year(query, limit=limit)
    return [title for title, _ in results]


# ─────────────────────────────────────────────────────────────
# PUBLIC: format_suggestion_label
# ─────────────────────────────────────────────────────────────

def format_suggestion_label(title: str, year: Optional[str]) -> str:
    """
    "Lucifer (2016)"  — when TMDB has a year
    "Dark"            — when TMDB confirms but has no year
    """
    display = title.title() if title == title.lower() else title
    return f"{display} ({year})" if year else display


# ─────────────────────────────────────────────────────────────
# TITLE NORMALIZER (used by indexer)
# ─────────────────────────────────────────────────────────────

def _normalize_title(name: str) -> str:
    clean = re.sub(r"[._\-]", " ", name)
    clean = re.sub(r"\[.*?\]|\(.*?\)", "", clean)
    match = SERIES_RE.search(clean)
    if match:
        clean = clean[:match.start()]
    clean = re.sub(
        r"\b(4K|1080P|720P|480P|360P|HDRIP|WEBRIP|WEB|BLURAY|X264|X265"
        r"|HEVC|AAC|DDP5\.1|REPACK|COMPLETE|BATCH)\b",
        "", clean, flags=re.IGNORECASE,
    )
    clean = re.sub(r"\b(19|20)\d{2}\b", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean.title()


# ─────────────────────────────────────────────────────────────
# PARSE SERIES INFO (used by bulk indexer)
# ─────────────────────────────────────────────────────────────

def parse_series_info(file_name: str):
    if not file_name:
        return "", 0, 0, "UNKNOWN", "Unknown"

    name  = file_name
    upper = name.upper()
    season = episode = 0

    m = re.search(r"(?i)S(\d{1,3})[^\d]?(?:EP|E)(\d{1,3})", name)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?i)Season\s?(\d+)\s?[-]?\s?Episode\s?(\d+)", name)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"(?i)\b(\d{1,2})x(\d{1,2})\b", name)
            if m:
                season, episode = int(m.group(1)), int(m.group(2))
            else:
                m = re.search(r"(?i)\bS(\d{1,3})\b", name)
                if m: season = int(m.group(1))
                m = re.search(r"(?i)\b(?:EP|E)(\d{1,3})\b", name)
                if m: episode = int(m.group(1))

    quality = "UNKNOWN"
    for q in ["4K", "1080P", "720P", "480P", "360P"]:
        if q in upper:
            quality = q
            break

    langs = set()
    for kw, lang in {
        "HINDI": "Hindi", "ENGLISH": "English", "TAMIL": "Tamil",
        "TELUGU": "Telugu", "MALAYALAM": "Malayalam", "KANNADA": "Kannada",
    }.items():
        if kw in upper: langs.add(lang)

    for pat, lang in {
        r"\bHIN\b": "Hindi",  r"\bENG\b": "English", r"\bTAM\b": "Tamil",
        r"\bTEL\b": "Telugu", r"\bMAL\b": "Malayalam", r"\bKAN\b": "Kannada",
    }.items():
        if re.search(pat, upper): langs.add(lang)

    if "DUAL" in upper or "MULTI" in upper:
        return _normalize_title(name), season, episode, quality, "Multi Audio"

    if len(langs) == 1:   language = next(iter(langs))
    elif len(langs) > 1:  language = " + ".join(sorted(langs))
    else:                 language = "Unknown"

    return _normalize_title(name), season, episode, quality, language


# ─────────────────────────────────────────────────────────────
# BUTTON OWNER CHECK
# ─────────────────────────────────────────────────────────────

async def check_owner(q, uid: int):
    if q.from_user.id != int(uid):
        name = q.from_user.first_name or "User"
        await q.answer(f"Hey {name}, this button is not for you.", show_alert=True)
        return False
    return True
