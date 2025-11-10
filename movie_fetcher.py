import requests
import os
from config import OMDB_API_KEY

def get_imdb_data(query: str):
    try:
        if not OMDB_API_KEY:
            return None

        # try exact title first
        m = requests.get("https://www.omdbapi.com/", params={
            "apikey": OMDB_API_KEY,
            "t": query
        }).json()

        # fallback to search
        if m.get("Response") != "True":
            s = requests.get("https://www.omdbapi.com/", params={
                "apikey": OMDB_API_KEY,
                "s": query
            }).json()

            if s.get("Response") != "True":
                return None

            imdb_id = s["Search"][0]["imdbID"]
            m = requests.get("https://www.omdbapi.com/", params={
                "apikey": OMDB_API_KEY,
                "i": imdb_id
            }).json()

        poster = m.get("Poster")
        if poster and poster.startswith("http://"):
            poster = poster.replace("http://", "https://")

        return {
            "title": m.get("Title", query),
            "year": m.get("Year", "?"),
            "genres": m.get("Genre", "?"),
            "rating": m.get("imdbRating", "?"),
            "poster": poster,
            "link": f"https://www.imdb.com/title/{m.get('imdbID')}/"
        }

    except:
        return None
