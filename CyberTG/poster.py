import aiohttp

from config import (
    TMDB_API_KEY,
    OMDB_API_KEY,
    TMDB_IMAGE_BASE
)

from utils.poster_cache import (
    get_cached,
    save_cache
)


# -------------------------------------------------
# TMDB FETCH (PRIORITY)
# -------------------------------------------------

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

    # TMDB gives genre IDs only
    genre = "Unknown"

    if not poster:
        return None

    return poster, year, genre, rating


# -------------------------------------------------
# OMDB FETCH (FALLBACK)
# -------------------------------------------------

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

    return poster, year, genre, rating


# -------------------------------------------------
# MAIN ENTRY
# -------------------------------------------------

async def get_series_info(series_name: str):
    """
    Main poster resolver.

    Priority:
        CACHE → TMDB → OMDB
    """

    # ---------- 1. CACHE ----------
    cached = get_cached(series_name)

    if cached:
        poster, year, genre, rating, source = cached
        return poster, year, genre, rating

    # ---------- 2. TMDB ----------
    tmdb = await fetch_tmdb(series_name)

    if tmdb:
        poster, year, genre, rating = tmdb

        save_cache(
            series_name,
            poster,
            year,
            genre,
            rating,
            "tmdb"
        )

        return poster, year, genre, rating

    # ---------- 3. OMDB FALLBACK ----------
    omdb = await fetch_omdb(series_name)

    if omdb:
        poster, year, genre, rating = omdb

        save_cache(
            series_name,
            poster,
            year,
            genre,
            rating,
            "omdb"
        )

        return poster, year, genre, rating

    # ---------- NOTHING FOUND ----------
    return None, None, None, None
