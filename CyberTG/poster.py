import aiohttp

from config import TMDB_API_KEY, OMDB_API_KEY
from utils.poster_cache import get_cached, save_cache

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


# -------------------------------------------------
# TMDB FETCH
# -------------------------------------------------

async def fetch_tmdb(series_name):

    url = "https://api.themoviedb.org/3/search/tv"

    params = {
        "api_key": TMDB_API_KEY,
        "query": series_name
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            data = await r.json()

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

    return poster, year, genre, rating


# -------------------------------------------------
# OMDB FETCH
# -------------------------------------------------

async def fetch_omdb(series_name):

    url = "http://www.omdbapi.com/"

    params = {
        "apikey": OMDB_API_KEY,
        "t": series_name,
        "type": "series"
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            data = await r.json()

    if data.get("Response") != "True":
        return None

    poster = data.get("Poster")
    year = data.get("Year")
    genre = data.get("Genre")
    rating = data.get("imdbRating")

    return poster, year, genre, rating


# -------------------------------------------------
# MAIN ENTRY
# -------------------------------------------------

async def get_series_info(series_name):

    # ---------- CACHE FIRST ----------
    cached = get_cached(series_name)

    if cached:
        poster, year, genre, rating, source = cached
        return poster, year, genre, rating

    # ---------- TRY TMDB ----------
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

    # ---------- FALLBACK OMDB ----------
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
