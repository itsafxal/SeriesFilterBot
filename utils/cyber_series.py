"""
cyber_series.py — SMART SUGGESTION ENGINE
==========================================
FIXES:
  1. _normalize_title — now collapses "Ss Lucifer", "Lucifer S05", "Lucifer"
     all down to "Lucifer" via a single ordered strip pipeline.
  2. _API_CACHE bounded LRU (OrderedDict, cap = 2048) — no memory leak.
  3. Minor: deduplicated imports, tightened regex literals.
  4. SERIES_RE is now built from the same comprehensive PATTERNS library
     used by the indexer, ensuring every known season/episode token is
     stripped during title normalisation.
"""

import re
import asyncio
import time
from collections import OrderedDict
from typing import List, Optional, Tuple

from DB.database import search_series
from utils.cyber_flood import safe_answer


# ─────────────────────────────────────────────────────────────
# COMPREHENSIVE SEASON/EPISODE PATTERNS  (shared with indexer)
# ─────────────────────────────────────────────────────────────
from utils.cyber_index import PATTERNS


def _build_series_stripper() -> re.Pattern:
    """Combine all PATTERNS into a single regex for stripping."""
    parts = []
    for pat in PATTERNS.values():
        parts.append(pat.pattern)
    return re.compile("|".join(parts), re.IGNORECASE)


SERIES_RE = _build_series_stripper()

# Lone "S" or "SS" remnants
_LONE_S_RE = re.compile(r"(?<!\w)[Ss]{1,2}(?!\w)")

# Part/P-number suffixes like S05P2, Part2, Pt1 — strip these early
_PART_RE = re.compile(r"\b(?:[Ss]\d{1,3})?[Pp](?:art|t)?[\s.\-_]?\d{1,2}\b", re.IGNORECASE)

# Video quality / codec / release-group noise
_JUNK_RE = re.compile(
    r"\b(4K|2160P|1080P|720P|480P|360P|HDRIP|WEBRIP|WEB[-]?DL|BLURAY|BDRIP"
    r"|X264|X265|HEVC|AVC|AAC|AC3|DDP5\.1|DTS|REPACK|PROPER|COMPLETE|BATCH"
    r"|NF|AMZN|DSNP|HMAX|HDTV|PDTV|DVDRip)\b",
    re.IGNORECASE,
)

_YEAR_RE  = re.compile(r"\b(19|20)\d{2}\b")
_SPACE_RE = re.compile(r"\s{2,}")


def _normalize_title(name: str) -> str:
    """
    Strips separators, brackets, episode codes, quality tags, and stray
    season-letter remnants so that all of these collapse to "Lucifer":
        Lucifer.S05E01.1080p
        Ss Lucifer
        LUCIFER S05
        lucifer.complete
    """
    text = re.sub(r"[._\-]+", " ", name)
    text = re.sub(r"\[.*?\]|\(.*?\)", "", text)
    text = _PART_RE.sub(" ", text)
    text = SERIES_RE.sub(" ", text)
    text = _LONE_S_RE.sub(" ", text)
    text = _JUNK_RE.sub(" ", text)
    text = _YEAR_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text.title() if text else ""


def clean_query(q: str) -> str:
    if not q:
        return ""
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = SERIES_RE.sub("", q)
    q = _LONE_S_RE.sub("", q)
    q = _SPACE_RE.sub(" ", q).strip()
    return q


# ── LRU cache for API lookups ─────────────────────────────────
_API_CACHE_MAX = 2048
_API_CACHE_TTL = 6 * 3600
_API_CACHE: OrderedDict[str, dict] = OrderedDict()
_API_SEM = asyncio.Semaphore(3)


def _cache_get(key: str) -> dict | None:
    entry = _API_CACHE.get(key)
    if entry is None:
        return None
    if (time.time() - entry["ts"]) >= _API_CACHE_TTL:
        _API_CACHE.pop(key, None)
        return None
    _API_CACHE.move_to_end(key)
    return entry


def _cache_set(key: str, value: dict) -> None:
    if key in _API_CACHE:
        _API_CACHE.move_to_end(key)
    _API_CACHE[key] = value
    if len(_API_CACHE) > _API_CACHE_MAX:
        _API_CACHE.popitem(last=False)


async def _lookup_title(title: str) -> Tuple[Optional[str], bool]:
    key   = title.lower().strip()
    entry = _cache_get(key)
    if entry is not None:
        return entry["year"], entry["found"]

    year  = None
    found = False

    async with _API_SEM:
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


async def _sql_candidates(query: str, limit: int = 8) -> List[str]:
    """FTS5 search with noise-stripping & smart fallback handled internally."""
    results = await search_series(query)
    return list(results)[:limit] if results else []





async def _validate_and_enrich(candidates: List[str]) -> List[Tuple[str, Optional[str]]]:
    lookups = await asyncio.gather(
        *[_lookup_title(t) for t in candidates],
        return_exceptions=True,
    )
    return [
        (title, result[0] if not isinstance(result, Exception) else None)
        for title, result in zip(candidates, lookups)
    ]


async def suggest_series_with_year(query: str, limit: int = 4) -> List[Tuple[str, Optional[str]]]:
    q = clean_query(query)
    if not q:
        return []
    candidates = await _sql_candidates(q, limit=limit * 4)
    if not candidates:
        return []

    # Normalize and deduplicate — collapse variants like
    # "Stranger Things S05P2" and "Stranger Things Tales From"
    # down to their shared base "Stranger Things".
    seen: set[str] = set()
    unique: List[str] = []
    for c in candidates:
        normalized = _normalize_title(c)
        k = normalized.lower()
        if not k:
            continue
        # Check if this normalized title is a prefix-duplicate of one already
        # seen, or vice-versa — keep only the shorter (base) form.
        dominated = False
        to_remove = []
        for existing in list(seen):
            if existing.startswith(k):
                # new one is shorter/more-base: remove existing, add new
                to_remove.append(existing)
            elif k.startswith(existing):
                # existing is already shorter: skip new
                dominated = True
                break
        if dominated:
            continue
        for r in to_remove:
            seen.discard(r)
            unique = [u for u in unique if u.lower() != r]
        seen.add(k)
        unique.append(normalized)

    validated = await _validate_and_enrich(unique)
    return validated[:limit]


async def suggest_series(query: str, limit: int = 4) -> List[str]:
    results = await suggest_series_with_year(query, limit=limit)
    return [title for title, _ in results]


def format_suggestion_label(title: str, year: Optional[str]) -> str:
    display = title.title() if title == title.lower() else title
    return f"{display} ({year})" if year else display


# ── Button owner check ────────────────────────────────────────
async def check_owner(q, uid: int) -> bool:
    if q.from_user.id != int(uid):
        name = q.from_user.first_name or "User"
        await safe_answer(q, f"Hey {name}, this button is not for you.", show_alert=True)
        return False
    return True
