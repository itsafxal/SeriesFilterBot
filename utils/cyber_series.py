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
# COMPREHENSIVE SEASON/EPISODE PATTERNS  (shared with indexer)
# ─────────────────────────────────────────────────────────────

PATTERNS = {
    # Standard
    "S01E01": re.compile(r"\b[sS](\d{1,3})[.\-_\s]?[eE](\d{1,3})\b"),
    "01x01": re.compile(r"\b(\d{1,3})[xX](\d{1,3})\b"),
    "Season Episode": re.compile(r"\bseason[.\-_\s]*(\d{1,3})[.\-_\s]*episode[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Season Ep": re.compile(r"\bseason[.\-_\s]*(\d{1,3})[.\-_\s]*(?:ep|e)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "S01 EP01": re.compile(r"\b[sS](\d{1,3})[.\-_\s]*(?:ep|e)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "S01-E01": re.compile(r"\b[sS](\d{1,3})-(?:[eE])(\d{1,3})\b"),
    "S01.E01": re.compile(r"\b[sS](\d{1,3})\.(?:[eE])(\d{1,3})\b"),
    "S01_E01": re.compile(r"\b[sS](\d{1,3})_(?:[eE])(\d{1,3})\b"),
    "S01 E01": re.compile(r"\b[sS](\d{1,3})\s+(?:[eE])(\d{1,3})\b"),

    # Multi episodes
    "S01E01E02": re.compile(r"\b[sS](\d{1,3})[.\-_\s]*[eE](\d{1,3})(?:[eE](\d{1,3}))+"),
    "S01E01-E05": re.compile(r"\b[sS](\d{1,3})[.\-_\s]*[eE](\d{1,3})[\-~to]+(?:[eE])?(\d{1,3})"),
    "01x01-01x05": re.compile(r"\b(\d{1,3})[xX](\d{1,3})[\-~](\d{1,3})[xX](\d{1,3})\b"),

    # Three / four digit
    "101": re.compile(r"\b([1-9])(\d{2})\b"),          # season 1, episode 01
    "1001": re.compile(r"\b(\d{2})(\d{2})\b"),         # season 10, episode 01
    "0101": re.compile(r"\b(\d{2})(\d{2})\b"),         # same as above – last resort

    # Episode only
    "Episode 01": re.compile(r"\bepisode[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Ep01": re.compile(r"\bep[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "E01": re.compile(r"\b[eE](\d{1,3})\b"),

    # Season only
    "Season 01": re.compile(r"\bseason[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "S01": re.compile(r"\b[sS](\d{1,3})(?![eE])\b"),

    # Complete / pack
    "Complete Season": re.compile(r"\b(?:complete|full)[.\-_\s]*season[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Season Pack": re.compile(r"\bseason[.\-_\s]*(\d{1,3})[.\-_\s]*(?:pack|collection)\b", re.IGNORECASE),
    "Batch": re.compile(r"\bbatch[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),

    # Anime
    "Anime 01v2": re.compile(r"\b(\d{1,3})v\d+\b", re.IGNORECASE),
    "S01E01v2": re.compile(r"\b[sS](\d{1,3})[eE](\d{1,3})v\d+\b", re.IGNORECASE),
    "Episode 01v2": re.compile(r"\b(?:ep|episode)[.\-_\s]*(\d{1,3})v\d+\b", re.IGNORECASE),
    "OVA": re.compile(r"\bOVA[.\-_\s]*(\d{1,3})?\b", re.IGNORECASE),
    "ONA": re.compile(r"\bONA[.\-_\s]*(\d{1,3})?\b", re.IGNORECASE),
    "Special": re.compile(r"\bspecial[.\-_\s]*(\d{1,3})?\b", re.IGNORECASE),

    # Part / Chapter
    "Part 01": re.compile(r"\b(?:part|pt)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Chapter 01": re.compile(r"\b(?:chapter|chap)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),
    "Volume": re.compile(r"\b(?:vol|volume)[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),

    # Date based (stripping them but not parsing)
    "2024.01.15": re.compile(r"\b(20\d{2})[.\-_](\d{2})[.\-_](\d{2})\b"),
    "2024-01-15": re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"),

    # Mini series
    "Mini Series Part": re.compile(r"\bmini[.\-_\s]*series[.\-_\s]*(?:part)?[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),

    # Roman numerals
    "Season Roman": re.compile(r"\bseason[.\-_\s]*(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b", re.IGNORECASE),

    # WEB
    "WEB Episode": re.compile(r"\bweb[.\-_\s]*dl[.\-_\s]*ep[.\-_\s]*(\d{1,3})\b", re.IGNORECASE),

    # Korean/Japanese
    "EP 01 END": re.compile(r"\bep?[.\-_\s]*(\d{1,3})[.\-_\s]*(?:end|finale)\b", re.IGNORECASE),

    # Disc / CD
    "Disc 1": re.compile(r"\bdisc[.\-_\s]*(\d{1,2})\b", re.IGNORECASE),
    "CD1": re.compile(r"\bcd[.\-_\s]*(\d{1,2})\b", re.IGNORECASE),

    # Absolute numbering (dangerous – used as last resort)
    "Naruto 220": re.compile(r"\b(0?\d{1,4})\b"),

    # Range
    "Episodes 1-10": re.compile(r"\bepisodes?[.\-_\s]*(\d{1,3})[\-~to]+(\d{1,3})\b", re.IGNORECASE),

    # Special editions (no numbers)
    "Director Cut": re.compile(r"\bdirector'?s?[.\-_\s]*cut\b", re.IGNORECASE),
    "Extended": re.compile(r"\bextended\b", re.IGNORECASE),
    "Uncut": re.compile(r"\buncut\b", re.IGNORECASE),
}


def _build_series_stripper() -> re.Pattern:
    """Combine all PATTERNS into a single regex for stripping."""
    parts = []
    for pat in PATTERNS.values():
        parts.append(pat.pattern)
    # Join patterns with | to create one giant alternation
    return re.compile("|".join(parts), re.IGNORECASE)

# Replace the old monolithic SERIES_RE with the dynamically built one
SERIES_RE = _build_series_stripper()


# Lone "S" or "SS" remnants (kept from original)
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

    # 3. Remove full season+episode tokens (now uses the comprehensive patterns)
    text = SERIES_RE.sub(" ", text)

    # 4. Remove lone "S" / "SS" that SERIES_RE leaves behind
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
        normalized = _normalize_title(c)
        k = normalized.lower()
        if k not in seen:
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


# ─────────────────────────────────────────────────────────────
# PARSE SERIES INFO  (kept as a convenience – now uses the patterns too)
# ─────────────────────────────────────────────────────────────

_LANG_KW = {
    "HINDI": "Hindi", "ENGLISH": "English", "TAMIL": "Tamil",
    "TELUGU": "Telugu", "MALAYALAM": "Malayalam", "KANNADA": "Kannada",
}
_LANG_PAT = {
    r"\bHIN\b": "Hindi",  r"\bENG\b": "English", r"\bTAM\b": "Tamil",
    r"\bTEL\b": "Telugu", r"\bMAL\b": "Malayalam", r"\bKAN\b": "Kannada",
}

# Roman numeral helper (same as indexer)
ROMAN_TO_INT = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10
}

def parse_series_info(file_name: str):
    if not file_name:
        return "", 0, 0, "UNKNOWN", "Unknown"

    name  = file_name
    upper = name.upper()
    season = episode = 0

    # Use the same priority order as the indexer
    ORDER = [
        "S01E01E02", "S01E01-E05", "01x01-01x05",
        "S01E01", "S01-E01", "S01.E01", "S01_E01", "S01 E01",
        "S01 EP01",
        "Season Episode", "Season Ep",
        "01x01",
        "Episode 01", "Ep01", "E01",
        "Season 01", "S01",
        "Complete Season", "Season Pack", "Batch",
        "Anime 01v2", "S01E01v2", "Episode 01v2",
        "OVA", "ONA", "Special",
        "Part 01", "Chapter 01", "Volume",
        "2024.01.15", "2024-01-15",
        "Mini Series Part",
        "Season Roman",
        "WEB Episode", "EP 01 END", "Disc 1", "CD1",
        "Episodes 1-10",
        "101", "1001", "0101",
    ]

    for key in ORDER:
        pat = PATTERNS.get(key)
        if not pat:
            continue
        m = pat.search(name)
        if m:
            groups = m.groups()
            if key in ("S01E01", "S01-E01", "S01.E01", "S01_E01", "S01 E01",
                       "S01 EP01", "Season Episode", "Season Ep", "01x01",
                       "S01E01v2"):
                season = int(groups[0])
                episode = int(groups[1])
                break
            elif key == "S01E01E02":
                season = int(groups[0])
                episode = int(groups[1])
                break
            elif key == "S01E01-E05":
                season = int(groups[0])
                episode = int(groups[1])
                break
            elif key == "01x01-01x05":
                season = int(groups[0])
                episode = int(groups[1])
                break
            elif key in ("Episode 01", "Ep01", "E01",
                         "Anime 01v2", "Episode 01v2",
                         "Part 01", "Chapter 01", "Volume",
                         "Mini Series Part", "WEB Episode",
                         "EP 01 END", "Disc 1", "CD1"):
                if not season:
                    season = 0
                episode = int(groups[0])
                break
            elif key in ("Season 01", "S01", "Complete Season",
                         "Season Pack", "Batch"):
                season = int(groups[0])
                break
            elif key in ("OVA", "ONA", "Special"):
                ova_num = groups[0] if groups[0] else None
                if ova_num:
                    episode = int(ova_num)
                break
            elif key in ("2024.01.15", "2024-01-15"):
                pass
            elif key == "Season Roman":
                roman = groups[0].lower()
                season = ROMAN_TO_INT.get(roman, 0)
                break
            elif key == "Episodes 1-10":
                episode = int(groups[0])
                break
            elif key in ("101", "1001", "0101"):
                digits = groups[0] + groups[1]
                if len(digits) == 3:
                    season = int(digits[0])
                    episode = int(digits[1:])
                else:
                    season = int(digits[:2])
                    episode = int(digits[2:])
                break

    # Quality (unchanged)
    quality = next(
        (q for q in ("4K", "2160P", "1080P", "720P", "480P", "360P") if q in upper),
        "UNKNOWN",
    )

    # Language (unchanged)
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
