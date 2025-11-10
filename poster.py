# utils/poster.py
import aiohttp
import asyncio
import shelve

try:
    from config import OMDB_API_KEY, DEFAULT_POSTER, OMDB_CACHE_DB
except Exception:
    OMDB_API_KEY = None
    DEFAULT_POSTER = None
    OMDB_CACHE_DB = "/tmp/omdb_poster_cache.db"

_cache = {}
DISK_CACHE_FILE = OMDB_CACHE_DB or "/tmp/omdb_poster_cache.db"

async def get_series_poster_async(title: str, use_disk_cache: bool = True, timeout: int = 6) -> str:
    """
    Query OMDb for a series poster (type=series). Return poster URL or DEFAULT_POSTER if none found.
    """
    if not title:
        return DEFAULT_POSTER

    key = title.strip().lower()
    if key in _cache:
        return _cache[key]

    if use_disk_cache:
        try:
            with shelve.open(DISK_CACHE_FILE) as db:
                if key in db:
                    _cache[key] = db[key]
                    return db[key]
        except Exception:
            pass

    if not OMDB_API_KEY:
        return DEFAULT_POSTER

    url = "http://www.omdbapi.com/"
    params = {"apikey": OMDB_API_KEY, "t": title, "type": "series"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=timeout) as resp:
                if resp.status != 200:
                    return DEFAULT_POSTER
                data = await resp.json()
    except asyncio.TimeoutError:
        return DEFAULT_POSTER
    except Exception:
        return DEFAULT_POSTER

    if data.get("Response", "False") == "True":
        poster = data.get("Poster")
        if poster and poster != "N/A":
            _cache[key] = poster
            if use_disk_cache:
                try:
                    with shelve.open(DISK_CACHE_FILE) as db:
                        db[key] = poster
                except Exception:
                    pass
            return poster

    return DEFAULT_POSTER
