import requests
from typing import Optional, Dict, Any
from config import OMDB_API_KEY


def fetch_omdb(title: str, year: Optional[str] = None, timeout: int = 8) -> Dict[str, Any]:
    """
    OMDb fetch — tries exact title first (t=),
    if not found → fallback to search mode (s=) and then fetch full data (i=)
    """
    if not OMDB_API_KEY:
        raise RuntimeError("OMDb API key not configured.")

    def _request(params):
        r = requests.get("https://www.omdbapi.com/", params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # ------------ 1) TRY EXACT TITLE (t=) ------------
    params = {"t": title, "type": "series", "apikey": OMDB_API_KEY}
    
    if year:
        params["y"] = str(year)

    data = _request(params)

    # retry exact title without year
    if data.get("Response") == "False" and year:
        params.pop("y", None)
        data = _request(params)

    # ------------ 2) IF FAIL → DO SEARCH (s=) ------------
    if data.get("Response") == "False":
        s = _request({"s": title, "type": "series", "apikey": OMDB_API_KEY})
        if s.get("Response") != "True":
            raise RuntimeError(f"OMDb: {s.get('Error','Movie not found')}")

        imdb_id = s["Search"][0]["imdbID"]

        data = _request({"i": imdb_id, "apikey": OMDB_API_KEY})

    poster = data.get("Poster")
    if poster and poster.startswith("http://"):
        poster = poster.replace("http://", "https://")

    return {
        "title": data.get("Title"),
        "year": data.get("Year"),
        "rating": data.get("imdbRating"),
        "genres": (data.get("Genre") or "").split(", ") if data.get("Genre") else [],
        "plot": data.get("Plot"),
        "poster": poster,
        "imdb_id": data.get("imdbID")
    }


# backward compatibility for your old code — you can delete get_imdb_data calls slowly
def get_imdb_data(query: str):
    try:
        return fetch_omdb(query)
    except:
        return None
