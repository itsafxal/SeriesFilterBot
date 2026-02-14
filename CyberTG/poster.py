import aiohttp

from config import TMDB_API_KEY
from utils.tmdb_cache import (
    get_cached_tmdb,
    save_cached_tmdb
)

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


# -------------------------------------------------
# TMDB + SQLITE CACHE
# -------------------------------------------------

async def get_series_info(series_name: str):

    # ---------- CACHE CHECK ----------
    cached = get_cached_tmdb(series_name)

    if cached:
        poster, year, genre, rating = cached
        return poster, year, genre, rating

    # ---------- TMDB REQUEST ----------
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
        return None, None, None, None

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

    # ---------- SAVE CACHE ----------
    save_cached_tmdb(
        series_name,
        poster,
        year,
        genre,
        rating
    )

    return poster, year, genre, rating
