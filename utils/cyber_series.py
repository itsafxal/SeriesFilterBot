"""
cyber_series.py — SMART SUGGESTION ENGINE
==========================================
FIXES:
  1. _normalize_title — now collapses "Ss Lucifer", "Lucifer S05", "Lucifer"
     all down to "Lucifer" via a single ordered strip pipeline.
  2. _API_CACHE bounded LRU (OrderedDict, cap = 2048) — no memory leak.
  3. Minor: deduplicated imports, tightened regex literals.
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
# SERIES / EPISODE REGEX  (shared, compiled once)
# ─────────────────────────────────────────────────────────────

# Matches any season/episode token we want to REMOVE from a display title.
# Order matters: longest/most-specific patterns first.
SERIES_RE = re.compile(
    r"(?i)\b("
    r"(19|20)\d{2}[\s._-]*S\d{1,3}E\d{1,3}"   # 2023.S01E01
    r"|S\d{1,3}E\d{1,3}E\d{1,3}"               # S01E01E02
    r"|S\d{1,3}\s?E\d{1,3}"                    # S01 E01
    r"|S\d{1,3}\s?EP\d{1,3}"                   # S01 EP01
    r"|Season\s?\d+\s?[-]?\s?Episode\s?\d+"    # Season 1 Episode 2
    r"|Season\s?\d+"                            # Season 1
    r"|\d{1,2}x\d{1,2}"                        # 1x02
    r"|EP\d{1,3}"                               # EP03
    r"|E\d{1,3}(-E\d{1,3})?"                   # E01 / E01-E03
    r"|S\d{1,3}"                               # S01  ← keep LAST (shortest)
    r"|Combined"
    r")\b",
    re.IGNORECASE,
)

# Lone "S" or "SS" that are left over after the above strips (e.g. "Ss Lucifer")
_LONE_S_RE = re.compile(r"(?<!\w)[Ss]{1,2}(?!\w)")

# Video quality / codec / release-group noise
_JUNK_RE = re.compile(
    r"\b(4K|2160P|1080P|720P|480P|360P|HDRIP|WEBRIP|WEB[-]?DL|BLURAY|BDRIP"
    r"|X264|X265|HEVC|AVC|AAC|AC3|DDP5\.1|DTS|REPACK|PROPER|COMPLETE|BATCH"
    r"|NF|AMZN|DSNP|HMAX|HDTV|PDTV|DVDRip)\b",
    re.IGNORECASE,
)

_YEAR_RE  = re.compile(r"\b(19|20)\d{2}\b")
_SPACE_RE = re.compile(r"\s{2,}")


# ─────────────────────────────────────────────────────────────
# TITLE NORMALIZER  ← main fix
# ─────────────────────────────────────────────────────────────

def _normalize_title(name: str) -> str:
    """
    Strips separators, brackets, episode codes, quality tags, and stray
    season-letter remnants so that all of these collapse to "Lucifer":
        Lucifer.S05E01.1080p
        Ss Lucifer
        LUCIFER S05
        lucifer.complete
    """
    # 1. Replace common separators with spaces
    text = re.sub(r"[._\-]+", " ", name)

    # 2. Drop bracketed/parenthesised blocks  [720p]  (Hindi)
    text = re.sub(r"\[.*?\]|\(.*?\)", "", text)

    # 3. Remove full season+episode tokens (S01E02, Season 1 Episode 2, …)
    text = SERIES_RE.sub(" ", text)

    # 4. Remove lone "S" / "SS" that SERIES_RE leaves behind
    #    e.g. "Ss Lucifer" → " Lucifer",  "Lucifer S" → "Lucifer "
    text = _LONE_S_RE.sub(" ", text)

    # 5. Strip quality / codec noise
    text = _JUNK_RE.sub(" ", text)

    # 6. Strip bare years
    text = _YEAR_RE.sub(" ", text)

    # 7. Collapse whitespace and title-case
    text = _SPACE_RE.sub(" ", text).strip()
    return text.title() if text else ""


# ─────────────────────────────────────────────────────────────
# QUERY CLEANER
# ─────────────────────────────────────────────────────────────

def clean_query(q: str) -> str:
    if not q:
        return ""
    q = q.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = SERIES_RE.sub("", q)
    q = _LONE_S_RE.sub("", q)
    q = _SPACE_RE.sub(" ", q).strip()
    return q


# ─────────────────────────────────────────────────────────────
# LRU CACHE FOR API LOOKUPS
# ─────────────────────────────────────────────────────────────

_API_CACHE_MAX = 2048
_API_CACHE_TTL = 6 * 3600
_API_CACHE: OrderedDict[str, dict] = OrderedDict()
_API_SEM   = asyncio.Semaphore(3)


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


# ─────────────────────────────────────────────────────────────
# TMDB / OMDB ENRICHMENT
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# STEP 1 — SQL CANDIDATES
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
# STEP 2 — FUZZY FALLBACK
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
# STEP 3 — TMDB/OMDB ENRICHMENT
# ─────────────────────────────────────────────────────────────

async def _validate_and_enrich(
    candidates: List[str],
) -> List[Tuple[str, Optional[str]]]:
    lookups = await asyncio.gather(
        *[_lookup_title(t) for t in candidates],
        return_exceptions=True,
    )
    return [
        (title, result[0] if not isinstance(result, Exception) else None)
        for title, result in zip(candidates, lookups)
    ]


# ─────────────────────────────────────────────────────────────
# PUBLIC API
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

    seen: set[str] = set()
    unique: List[str] = []
    for c in candidates:
        k = c.lower().strip()
        if k not in seen:
            seen.add(k)
            unique.append(c)

    validated = await _validate_and_enrich(unique)
    return validated[:limit]


async def suggest_series(query: str, limit: int = 4) -> List[str]:
    results = await suggest_series_with_year(query, limit=limit)
    return [title for title, _ in results]


def format_suggestion_label(title: str, year: Optional[str]) -> str:
    display = title.title() if title == title.lower() else title
    return f"{display} ({year})" if year else display


# ─────────────────────────────────────────────────────────────
# PARSE SERIES INFO
# ─────────────────────────────────────────────────────────────

_LANG_KW = {
    "HINDI": "Hindi", "ENGLISH": "English", "TAMIL": "Tamil",
    "TELUGU": "Telugu", "MALAYALAM": "Malayalam", "KANNADA": "Kannada",
}
_LANG_PAT = {
    r"\bHIN\b": "Hindi",  r"\bENG\b": "English", r"\bTAM\b": "Tamil",
    r"\bTEL\b": "Telugu", r"\bMAL\b": "Malayalam", r"\bKAN\b": "Kannada",
}

def parse_series_info(file_name: str):
    if not file_name:
        return "", 0, 0, "UNKNOWN", "Unknown"

    name  = file_name
    upper = name.upper()
    season = episode = 0

    # Season + Episode
    for pattern, s_grp, e_grp in [
        (r"(?i)S(\d{1,3})[^\d]?(?:EP|E)(\d{1,3})", 1, 2),
        (r"(?i)Season\s?(\d+)\s?[-]?\s?Episode\s?(\d+)", 1, 2),
        (r"(?i)\b(\d{1,2})x(\d{1,2})\b", 1, 2),
    ]:
        m = re.search(pattern, name)
        if m:
            season, episode = int(m.group(s_grp)), int(m.group(e_grp))
            break
    else:
        m = re.search(r"(?i)\bS(\d{1,3})\b", name)
        if m: season = int(m.group(1))
        m = re.search(r"(?i)\b(?:EP|E)(\d{1,3})\b", name)
        if m: episode = int(m.group(1))

    # Quality
    quality = next(
        (q for q in ("4K", "2160P", "1080P", "720P", "480P", "360P") if q in upper),
        "UNKNOWN",
    )

    # Language
    if "DUAL" in upper or "MULTI" in upper:
        return _normalize_title(name), season, episode, quality, "Multi Audio"

    langs = {lang for kw, lang in _LANG_KW.items() if kw in upper}
    langs |= {lang for pat, lang in _LANG_PAT.items() if re.search(pat, upper)}

    if   len(langs) == 1: language = next(iter(langs))
    elif len(langs) > 1:  language = " + ".join(sorted(langs))
    else:                 language = "Unknown"

    return _normalize_title(name), season, episode, quality, language


# ─────────────────────────────────────────────────────────────
# BUTTON OWNER CHECK
# ─────────────────────────────────────────────────────────────

async def check_owner(q, uid: int) -> bool:
    if q.from_user.id != int(uid):
        name = q.from_user.first_name or "User"
        await q.answer(f"Hey {name}, this button is not for you.", show_alert=True)
        return False
    return True
