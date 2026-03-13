"""
cyber_series.py — SMART SUGGESTION ENGINE  (FIXED)
====================================================
FIX: _API_CACHE memory leak — was unbounded (grew forever, one entry per unique
title lookup).  Now capped at _API_CACHE_MAX = 2048 entries using an OrderedDict
as an LRU cache.  Oldest entries are evicted when the cap is hit.

All other logic (SQL → fuzzy → TMDB/OMDB enrichment) unchanged.
"""

import re
import asyncio
import time
from collections import OrderedDict
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
# TMDB / OMDB LOOKUP  — LRU bounded cache
# FIX: was unbounded dict → now OrderedDict capped at _API_CACHE_MAX
# ─────────────────────────────────────────────────────────────

_API_CACHE_MAX = 2048   # max entries before eviction
_API_CACHE_TTL = 6 * 3600
_API_CACHE: OrderedDict[str, dict] = OrderedDict()   # key → {year, found, ts}
_API_SEM   = asyncio.Semaphore(3)


def _cache_get(key: str) -> dict | None:
    """LRU get — TTL checked here; evicts expired entry if stale."""
    entry = _API_CACHE.get(key)
    if entry is None:
        return None
    if (time.time() - entry["ts"]) >= _API_CACHE_TTL:
        _API_CACHE.pop(key, None)
        return None
    _API_CACHE.move_to_end(key)
    return entry


def _cache_set(key: str, value: dict):
    """LRU set — evicts oldest when cap is hit."""
    if key in _API_CACHE:
        _API_CACHE.move_to_end(key)
    _API_CACHE[key] = value
    if len(_API_CACHE) > _API_CACHE_MAX:
        _API_CACHE.popitem(last=False)  # remove LRU entry


async def _lookup_title(title: str) -> Tuple[Optional[str], bool]:
    """
    Check TMDB then OMDB for the given title.
    Returns (year, found).
    """
    key   = title.lower().strip()
    entry = _cache_get(key)
    if entry is not None:
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
                    params={"api_key": TMDB_API_KEY(), "query": title},
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

        # ── OMDB fallback ────────────────────────────────────
        if not found:
            try:
                import aiohttp
                from config import OMDB_API_KEY
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        "http://www.omdbapi.com/",
                        params={"apikey": OMDB_API_KEY(), "t": title, "type": "series"},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as r:
                        data = await r.json()
                if data.get("Response") == "True":
                    found    = True
                    year_raw = data.get("Year", "") or ""
                    year     = year_raw[:4] if len(year_raw) >= 4 else None
            except Exception:
                pass

    _cache_set(key, {"year": year, "found": found, "ts": time.time()})
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
# STEP 1 — SQL candidates
# ─────────────────────────────────────────────────────────────

async def _sql_candidates(query: str, limit: int = 8) -> List[str]:
    results = await search_series(query)
    if results:
        return list(results)[:limit]

    words = query.split()
    if len(words) > 1 and len(words[0]) >= 3:
        results = await search_series(words[0])
        if results:
            return list(results)[:limit]

    return []


# ─────────────────────────────────────────────────────────────
# STEP 2 — FUZZY fallback
# ─────────────────────────────────────────────────────────────

async def _fuzzy_candidates(query: str, limit: int = 6) -> List[str]:
    if not HAVE_RAPIDFUZZ:
        return []

    try:
        all_series = await get_all_series()
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
# STEP 3 — TMDB/OMDB enrichment
# ─────────────────────────────────────────────────────────────

async def _validate_and_enrich(
    candidates: List[str],
) -> List[Tuple[str, Optional[str]]]:
    lookups = await asyncio.gather(
        *[_lookup_title(t) for t in candidates],
        return_exceptions=True,
    )

    enriched = []
    for title, result in zip(candidates, lookups):
        if isinstance(result, Exception):
            enriched.append((title, None))
            continue
        year, _found = result
        enriched.append((title, year))

    return enriched


# ─────────────────────────────────────────────────────────────
# PUBLIC: suggest_series_with_year
# ─────────────────────────────────────────────────────────────

async def suggest_series_with_year(
    query: str, limit: int = 4
) -> List[Tuple[str, Optional[str]]]:
    q = clean_query(query)
    if not q:
        return []

    candidates = await _sql_candidates(q, limit=limit * 3)

    if not candidates:
        candidates = await _fuzzy_candidates(q, limit=limit * 2)

    if not candidates:
        return []

    seen, unique = set(), []
    for c in candidates:
        k = c.lower().strip()
        if k not in seen:
            seen.add(k)
            unique.append(c)

    validated = await _validate_and_enrich(unique)
    return validated[:limit]


# ─────────────────────────────────────────────────────────────
# PUBLIC: suggest_series (backward-compat)
# ─────────────────────────────────────────────────────────────

async def suggest_series(query: str, limit: int = 4) -> List[str]:
    results = await suggest_series_with_year(query, limit=limit)
    return [title for title, _ in results]


# ─────────────────────────────────────────────────────────────
# PUBLIC: format_suggestion_label
# ─────────────────────────────────────────────────────────────

def format_suggestion_label(title: str, year: Optional[str]) -> str:
    display = title.title() if title == title.lower() else title
    return f"{display} ({year})" if year else display


# ─────────────────────────────────────────────────────────────
# TITLE NORMALIZER
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
# PARSE SERIES INFO
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
