import requests
import html
from config import OMDB_API_KEY

def get_imdb_data(query: str):
    try:
        url = "https://www.omdbapi.com/"
        params = {
            "apikey": OMDB_API_KEY,
            "t": query,
            "plot": "short"
        }

        r = requests.get(url, params=params)
        data = r.json()

        if data.get("Response") != "True":
            return None

        poster = data.get("Poster")
        if poster and poster.startswith("http://"):
            poster = poster.replace("http://", "https://")

        return {
            "title": data.get("Title", query),
            "year": data.get("Year", "?"),
            "genres": data.get("Genre", ""),
            "rating": data.get("imdbRating", "?"),
            "poster": poster,
            "link": f"https://www.imdb.com/title/{data.get('imdbID')}/"
        }

    except Exception:
        return None
