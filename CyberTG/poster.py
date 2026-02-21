import aiohttp
from config import (
    TMDB_API_KEY,
    OMDB_API_KEY,
)
from utils.cyber_poster import (
    get_cached,
    save_cache
)

# High quality landscape backdrop size
TMDB_BACKDROP_BASE = "https://image.tmdb.org/t/p/w1280"
TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w780"


async def fetch_tmdb(series_name: str):
    """
    Fetch series info from TMDB using landscape backdrop.
    """

    url = "https://api.themoviedb.org/3/search/tv"

    params = {
        "api_key": TMDB_API_KEY,
        "query": series_name
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as r:
                data = await r.json()
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None

    item = results[0]

    backdrop_path = item.get("backdrop_path")
    poster_path = item.get("poster_path")

    # Prefer horizontal backdrop
    if backdrop_path:
        poster = f"{TMDB_BACKDROP_BASE}{backdrop_path}"
    elif poster_path:
        poster = f"{TMDB_POSTER_BASE}{poster_path}"
    else:
        return None

    year = (
        item.get("first_air_date", "")[:4]
        if item.get("first_air_date")
        else "N/A"
    )

    rating = str(item.get("vote_average", "N/A"))

    # Optional: You can expand this later using genre_ids
    genre = "Unknown"

    return poster, year, genre, rating


async def fetch_omdb(series_name: str):
    """
    Fallback to OMDB (usually vertical poster only).
    """

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
    except Exception:
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


async def get_series_info(series_name: str):
    """
    Main resolver with caching.
    """

    cached = get_cached(series_name)
    if cached:
        poster, year, genre, rating, source = cached
        return poster, year, genre, rating

    # Try TMDB first (better quality + landscape)
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

    # Fallback to OMDB
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

    return None, None, None, None


async def get_series_poster_async(series_name: str):
    """
    Legacy wrapper for old imports.
    """
    poster, year, genre, rating = await get_series_info(series_name)
    return poster
