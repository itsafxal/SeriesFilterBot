import aiohttp
from config import (
    TMDB_API_KEY,
    OMDB_API_KEY,
    TMDB_IMAGE_BASE
)
from utils.cyber_poster import (
    get_cached,
    save_cache
)


# =========================================================
# TMDB FETCH
# =========================================================

async def fetch_tmdb(series_name: str):

    url = "https://api.themoviedb.org/3/search/tv"

    params = {
        "api_key": TMDB_API_KEY,
        "query": series_name
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as r:
                data = await r.json()
    except:
        return None

    results = data.get("results", [])
    if not results:
        return None

    item = results[0]

    poster_path = item.get("poster_path")
    poster = (
        f"{TMDB_IMAGE_BASE}{poster_path}"
        if poster_path else None
    )

    year = (
        item.get("first_air_date", "")[:4]
        if item.get("first_air_date")
        else "N/A"
    )

    rating = str(item.get("vote_average", "N/A"))
    genre = "Unknown"

    # ------------------ LANGUAGE DETECTION ------------------

    lang_code = item.get("original_language", "")

    LANG_MAP = {
        "en": "English",
        "hi": "Hindi",
        "ml": "Malayalam",
        "ta": "Tamil",
        "te": "Telugu",
        "kn": "Kannada",
        "es": "Spanish",
        "fr": "French",
        "ko": "Korean",
        "ja": "Japanese",
        "zh": "Chinese",
        "de": "German"
    }

    language = LANG_MAP.get(lang_code, "Original Language")

    if not poster:
        return None

    return poster, year, genre, rating, language


# =========================================================
# OMDB FETCH (Fallback)
# =========================================================

async def fetch_omdb(series_name: str):

    url = "http://www.omdbapi.com/"

    params = {
        "apikey": OMDB_API_KEY,
        "t": series_name,
        "type": "series"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as r:
                data = await r.json()
    except:
        return None

    if data.get("Response") != "True":
        return None

    poster = data.get("Poster")
    if not poster or poster == "N/A":
        return None

    year = data.get("Year", "N/A")
    genre = data.get("Genre", "N/A")
    rating = data.get("imdbRating", "N/A")

    # ------------------ LANGUAGE ------------------

    language = data.get("Language", "Original Language")

    # Example: "Malayalam, Hindi"
    if "," in language:
        language = language.split(",")[0].strip()

    return poster, year, genre, rating, language


# =========================================================
# MAIN INFO FUNCTION (WITH CACHE)
# =========================================================

async def get_series_info(series_name: str):

    cached = get_cached(series_name)

    if cached:
        # Expected cached order:
        # poster, year, genre, rating, source, language
        poster, year, genre, rating, source, language = cached
        return poster, year, genre, rating, language

    # ---------- TMDB FIRST ----------
    tmdb = await fetch_tmdb(series_name)

    if tmdb:
        poster, year, genre, rating, language = tmdb

        save_cache(
            series_name,
            poster,
            year,
            genre,
            rating,
            "tmdb",
            language
        )

        return poster, year, genre, rating, language

    # ---------- OMDB FALLBACK ----------
    omdb = await fetch_omdb(series_name)

    if omdb:
        poster, year, genre, rating, language = omdb

        save_cache(
            series_name,
            poster,
            year,
            genre,
            rating,
            "omdb",
            language
        )

        return poster, year, genre, rating, language

    return None, None, None, None, None


# =========================================================
# LEGACY POSTER WRAPPER (Backward Compatible)
# =========================================================

async def get_series_poster_async(series_name: str):
    """
    Legacy wrapper for old imports.
    Returns only poster.
    """
    poster, _, _, _, _ = await get_series_info(series_name)
    return poster
