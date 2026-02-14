import aiohttp

from config import TMDB_API_KEY


TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


# -------------------------------------------------
# GET SERIES INFO FROM TMDB
# -------------------------------------------------

async def get_series_info(series_name: str):

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

    rating = item.get("vote_average", "N/A")

    # genre ids only → optional
    genre = "Unknown"

    return poster, year, genre, rating
