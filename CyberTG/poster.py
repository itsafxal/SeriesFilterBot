import aiohttp
from config import (
    TMDB_API_KEY,
    OMDB_API_KEY,
    TMDB_IMAGE_BASE,
    TMDB_IMAGE_BASE_WIDE,
)
from utils.cyber_poster import (
    get_cached,
    save_cache
)


# =========================================================
# TMDB FETCH
# =========================================================

async def fetch_tmdb(series_name: str, landscape: bool = False):
    url    = "https://api.themoviedb.org/3/search/tv"
    params = {"api_key": TMDB_API_KEY, "query": series_name}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None

    item = results[0]

    # Portrait → poster_path, Landscape → backdrop_path
    if landscape:
        img_path   = item.get("backdrop_path") or item.get("poster_path")
        image_base = TMDB_IMAGE_BASE_WIDE
    else:
        img_path   = item.get("poster_path")
        image_base = TMDB_IMAGE_BASE

    poster = f"{image_base}{img_path}" if img_path else None

    year = (
        item.get("first_air_date", "")[:4]
        if item.get("first_air_date") else "N/A"
    )
    rating    = str(item.get("vote_average", "N/A"))
    genre     = "Unknown"
    lang_code = item.get("original_language", "")

    LANG_MAP = {
        "en": "English", "hi": "Hindi", "ml": "Malayalam",
        "ta": "Tamil",   "te": "Telugu","kn": "Kannada",
        "bn": "Bengali", "mr": "Marathi","pa": "Punjabi",
        "es": "Spanish", "fr": "French", "ko": "Korean",
        "ja": "Japanese","zh": "Chinese","de": "German",
        "ar": "Arabic",  "tr": "Turkish","it": "Italian",
        "pt": "Portuguese","ru": "Russian","th": "Thai",
    }
    language = LANG_MAP.get(lang_code, "Unknown")

    if not poster:
        return None

    return poster, year, genre, rating, language


# =========================================================
# OMDB FETCH
# =========================================================

async def fetch_omdb(series_name: str):
    url    = "http://www.omdbapi.com/"
    params = {"apikey": OMDB_API_KEY, "t": series_name, "type": "series"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
    except Exception:
        return None

    if data.get("Response") != "True":
        return None

    poster = data.get("Poster")
    if not poster or poster == "N/A":
        return None

    year  = data.get("Year", "N/A")
    genre = data.get("Genre", "N/A")
    rating = data.get("imdbRating", "N/A")

    raw_langs = [l.strip() for l in data.get("Language", "").split(",") if l.strip()]
    KNOWN = {
        "Hindi","English","Tamil","Telugu","Malayalam","Kannada",
        "Bengali","Marathi","Punjabi","Korean","Japanese","Chinese",
        "Spanish","French","German","Arabic","Turkish","Italian",
        "Portuguese","Russian","Thai"
    }
    found_langs = [l for l in raw_langs if l in KNOWN]
    if not found_langs:
        language = raw_langs[0] if raw_langs else "Unknown"
    elif len(found_langs) == 1:
        language = found_langs[0]
    else:
        language = " + ".join(found_langs)

    return poster, year, genre, rating, language


# =========================================================
# MAIN INFO (BACKWARD SAFE)
# =========================================================

async def get_series_info(series_name: str, landscape: bool = False):
    """
    Returns: poster, year, genre, rating
    landscape=True  -> wide backdrop  (cached under "name:wide")
    landscape=False -> portrait poster (cached under "name")
    Each orientation cached separately so switching modes works correctly.
    """
    cache_key = f"{series_name}:wide" if landscape else series_name

    cached = get_cached(cache_key)
    if cached:
        poster, year, genre, rating, source, language = cached
        return poster, year, genre, rating

    tmdb = await fetch_tmdb(series_name, landscape=landscape)
    if tmdb:
        poster, year, genre, rating, language = tmdb
        save_cache(cache_key, poster, year, genre, rating, "tmdb", language)
        return poster, year, genre, rating

    # Landscape fallback: if no backdrop found, try portrait
    if landscape:
        tmdb_portrait = await fetch_tmdb(series_name, landscape=False)
        if tmdb_portrait:
            poster, year, genre, rating, language = tmdb_portrait
            save_cache(cache_key, poster, year, genre, rating, "tmdb", language)
            return poster, year, genre, rating

    omdb = await fetch_omdb(series_name)
    if omdb:
        poster, year, genre, rating, language = omdb
        save_cache(cache_key, poster, year, genre, rating, "omdb", language)
        return poster, year, genre, rating

    return None, None, None, None


# =========================================================
# LANGUAGE ACCESSOR
# =========================================================

async def get_series_language(series_name: str):
    cached = get_cached(series_name)
    if cached:
        _, _, _, _, _, language = cached
        return language

    tmdb = await fetch_tmdb(series_name)
    if tmdb:
        return tmdb[4]

    omdb = await fetch_omdb(series_name)
    if omdb:
        return omdb[4]

    return "Unknown"


# =========================================================
# LEGACY POSTER WRAPPER
# =========================================================

async def get_series_poster_async(series_name: str):
    poster, _, _, _ = await get_series_info(series_name)
    return poster
