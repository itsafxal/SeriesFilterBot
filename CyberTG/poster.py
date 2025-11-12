#poster.py

import aiohttp
import asyncio
import shelve
import re
from typing import Optional
import logging, aiohttp, asyncio

try:
    from config import OMDB_API_KEY, DEFAULT_POSTER, OMDB_CACHE_DB
except Exception:
    # fallback defaults (please update config.py with real values)
    OMDB_API_KEY = None
    DEFAULT_POSTER = None
    OMDB_CACHE_DB = "/tmp/omdb_cache.db"

_cache = {}
DISK_CACHE_FILE = OMDB_CACHE_DB or "/tmp/omdb_poster_cache.db"

def _normalize_title_for_omdb(title: str) -> str:
    """Light normalization: strip outer whitespace, remove trailing year (e.g. 2019), parentheses, and extra punctuation."""
    if not title:
        return title
    t = title.strip()
    # remove (TV) or similar trailing tags
    t = re.sub(r"\s*\(.*?\)\s*$", "", t)
    # remove trailing year like " - 2019" or " 2019"
    t = re.sub(r"[-\s]+(?:19|20)\d{2}$", "", t)
    return t.strip()

logging.basicConfig(level=logging.DEBUG)   # remove later if too noisy

async def debug_get_omdb(title, key, timeout=8):
    url = "https://www.omdbapi.com/"
    params = {"apikey": key, "t": title, "type": "series"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=timeout) as r:
                text = await r.text()
                logging.debug(f"[OMDB RAW {r.status}] params={params} text={text}")
                try:
                    return await r.json()
                except Exception:
                    return {"Response": "False", "Error": "non-json-response", "raw": text}
    except asyncio.TimeoutError:
        logging.warning("OMDb timeout")
        return {"Response":"False","Error":"timeout"}
    except Exception as e:
        logging.exception("OMDb network error")
        return {"Response":"False","Error": str(e)}

async def get_series_poster_async(title: str, use_disk_cache: bool = True, timeout: int = 6) -> Optional[str]:
    """
    Query OMDb for a series poster (type=series).
    Returns poster URL (string) or DEFAULT_POSTER (string) on failure.
    Caches results in-memory and (optionally) on-disk via shelve to avoid rate limits.
    """
    if not title:
        return DEFAULT_POSTER

    norm_title = _normalize_title_for_omdb(title)
    key = norm_title.strip().lower()

    # in-memory cache hit
    if key in _cache:
        return _cache[key]

    # disk cache hit
    if use_disk_cache:
        try:
            with shelve.open(DISK_CACHE_FILE) as db:
                if key in db:
                    _cache[key] = db[key]
                    return db[key]
        except Exception:
            # ignore cache read errors
            pass

    # no API key configured -> fallback
    if not OMDB_API_KEY:
        return DEFAULT_POSTER

    url = "http://www.omdbapi.com/"
    params = {"apikey": OMDB_API_KEY, "t": norm_title, "type": "series"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=timeout) as resp:
                if resp.status != 200:
                    return DEFAULT_POSTER
                data = await resp.json()
    except asyncio.TimeoutError:
        return DEFAULT_POSTER
    except Exception:
        # network/parse error
        return DEFAULT_POSTER

    # OMDb format: {"Response": "True", "Poster": "https://..."} or Response:"False"
    if data.get("Response", "False") == "True":
        poster = data.get("Poster")
        if poster and poster != "N/A":
            # cache and persist
            _cache[key] = poster
            if use_disk_cache:
                try:
                    with shelve.open(DISK_CACHE_FILE) as db:
                        db[key] = poster
                except Exception:
                    pass
            return poster

    return DEFAULT_POSTER
