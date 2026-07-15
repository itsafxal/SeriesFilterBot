import aiohttp
import re
from difflib import SequenceMatcher

from config import (
    TMDB_API_KEY,
    OMDB_API_KEY,
    TMDB_IMAGE_BASE,
    TMDB_IMAGE_BASE_WIDE,
)
from utils.cyber_poster import get_cached, save_cache


# =========================================================
# SMART MATCHING HELPERS
# =========================================================

def normalize(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.lower())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


# =========================================================
# TMDB FETCH (SMART MATCH + RETRY)
# =========================================================

async def fetch_tmdb(series_name: str, landscape: bool = False, retries: int = 2):
    url = "https://api.themoviedb.org/3/search/tv"

    for _ in range(retries):
        try:
            params = {
                "api_key": TMDB_API_KEY(),
                "query": series_name
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:

                    if r.status == 429:
                        continue

                    data = await r.json()

        except Exception:
            continue

        results = data.get("results", [])
        if not results:
            continue

        # =========================
        # 🧠 SMART MATCHING
        # =========================
        best_item = None
        best_score = 0

        for item in results[:10]:
            title = item.get("name") or item.get("original_name") or ""
            score = similarity(series_name, title)

            # Exact match boost
            if normalize(series_name) == normalize(title):
                score += 1.0

            # Year boost
            query_year = re.search(r'\b(19|20)\d{2}\b', series_name)
            item_year = (item.get("first_air_date") or "")[:4]

            if query_year and query_year.group(0) == item_year:
                score += 0.3

            # Optional regional boost
            lang = item.get("original_language")
            if lang in ["ml", "hi", "ta", "te"]:
                score += 0.1

            if score > best_score:
                best_score = score
                best_item = item

        # Reject weak matches
        if not best_item or best_score < 0.5:
            continue

        item = best_item

        # =========================
        # IMAGE SELECTION
        # =========================
        if landscape:
            img_path   = item.get("backdrop_path") or item.get("poster_path")
            image_base = TMDB_IMAGE_BASE_WIDE
        else:
            img_path   = item.get("poster_path")
            image_base = TMDB_IMAGE_BASE

        poster = f"{image_base}{img_path}" if img_path else None
        if not poster:
            continue

        year = item.get("first_air_date", "")[:4] if item.get("first_air_date") else "N/A"
        rating = str(item.get("vote_average", "N/A"))
        genre = "Unknown"

        lang_code = item.get("original_language", "")
        LANG_MAP = {
            "en": "English", "hi": "Hindi", "ml": "Malayalam",
            "ta": "Tamil", "te": "Telugu", "kn": "Kannada",
            "bn": "Bengali", "mr": "Marathi", "pa": "Punjabi",
            "es": "Spanish", "fr": "French", "ko": "Korean",
            "ja": "Japanese", "zh": "Chinese", "de": "German",
            "ar": "Arabic", "tr": "Turkish", "it": "Italian",
            "pt": "Portuguese", "ru": "Russian", "th": "Thai",
        }
        language = LANG_MAP.get(lang_code, "Unknown")

        plot = item.get("overview") or ""
        return poster, year, genre, rating, language, plot

    return None


# =========================================================
# OMDB FETCH (RETRY)
# =========================================================

async def fetch_omdb(series_name: str, retries: int = 2):
    url = "http://www.omdbapi.com/"

    for _ in range(retries):
        try:
            params = {
                "apikey": OMDB_API_KEY(),
                "t": series_name,
                "type": "series"
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    data = await r.json()

        except Exception:
            continue

        if data.get("Response") != "True":
            continue

        poster = data.get("Poster")
        if not poster or poster == "N/A":
            continue

        year = data.get("Year", "N/A")
        genre = data.get("Genre", "N/A")
        rating = data.get("imdbRating", "N/A")

        raw_langs = [l.strip() for l in data.get("Language", "").split(",") if l.strip()]
        language = raw_langs[0] if raw_langs else "Unknown"
        plot = data.get("Plot") or ""

        return poster, year, genre, rating, language, plot

    return None


# =========================================================
# MAIN INFO (SMART FALLBACK)
# =========================================================

async def get_series_info(series_name: str, landscape: bool = False):
    """
    Returns (poster_url, year, genre, rating, plot) or (None, None, None, None, "").
    Uses TMDB with smart matching, then OMDb as fallback.
    """
    cache_key = f"{series_name}:wide" if landscape else series_name

    cached = get_cached(cache_key)
    if cached:
        poster, year, genre, rating, source, language, plot = cached
        return poster, year, genre, rating, plot or ""

    # 1️⃣ TMDB (smart match)
    tmdb = await fetch_tmdb(series_name, landscape=landscape)
    if tmdb:
        poster, year, genre, rating, language, plot = tmdb
        save_cache(cache_key, poster, year, genre, rating, "tmdb", language, plot)
        return poster, year, genre, rating, plot

    # 2️⃣ Landscape fallback: if wide image not found, try standard poster
    if landscape:
        tmdb_portrait = await fetch_tmdb(series_name, landscape=False)
        if tmdb_portrait:
            poster, year, genre, rating, language, plot = tmdb_portrait
            save_cache(cache_key, poster, year, genre, rating, "tmdb", language, plot)
            return poster, year, genre, rating, plot

    # 3️⃣ OMDB fallback
    omdb = await fetch_omdb(series_name)
    if omdb:
        poster, year, genre, rating, language, plot = omdb
        save_cache(cache_key, poster, year, genre, rating, "omdb", language, plot)
        return poster, year, genre, rating, plot

    return None, None, None, None, ""


# =========================================================
# LANGUAGE FETCH
# =========================================================

async def get_series_language(series_name: str):
    """
    Returns the language of the series using TMDB or OMDB cache.
    """
    cached = get_cached(series_name)
    if cached:
        _, _, _, _, _, language, _ = cached
        return language

    tmdb = await fetch_tmdb(series_name)
    if tmdb:
        return tmdb[4]

    omdb = await fetch_omdb(series_name)
    if omdb:
        return omdb[4]

    return "Unknown"


# =========================================================
# SIMPLE POSTER WRAPPER
# =========================================================

async def get_series_poster_async(series_name: str):
    """
    Convenience wrapper; returns poster URL or None.
    """
    poster, _, _, _, _ = await get_series_info(series_name)
    return poster
