# CyberTG/poster.py

import aiohttp
import re

from config import OMDB_API_KEY, DEFAULT_POSTER

def _normalize_title_for_omdb(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    return title


async def get_series_info(title: str):
    """
    Returns: poster_url, year, genre, rating
    """
    if not OMDB_API_KEY:
        return DEFAULT_POSTER, None, None, None

    norm = _normalize_title_for_omdb(title)

    url = "http://www.omdbapi.com/"
    params = {
        "apikey": OMDB_API_KEY,
        "t": norm,
        "type": "series"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=7) as resp:
                data = await resp.json()
    except:
        return DEFAULT_POSTER, None, None, None

    if data.get("Response") != "True":
        return DEFAULT_POSTER, None, None, None

    poster = data.get("Poster") or DEFAULT_POSTER
    year = data.get("Year")
    genre = data.get("Genre")
    rating = data.get("imdbRating")

    return poster, year, genre, rating


# backward compatibility if used elsewhere
async def get_series_poster_async(title: str):
    poster, _, _, _ = await get_series_info(title)
    return poster
