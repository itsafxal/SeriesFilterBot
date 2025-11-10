import requests
import os

OMDB_API_KEY = os.getenv("OMDB_API_KEY")  # or hardcode your key

def get_imdb_data(query: str):
    try:
        if not OMDB_API_KEY:
            return None

        # 1) search
        s = requests.get("https://www.omdbapi.com/", params={
            "apikey": OMDB_API_KEY,
            "s": query
        }).json()

        if s.get("Response") != "True":
            return None

        imdb_id = s["Search"][0]["imdbID"]    # first match

        # 2) full
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
            "link": f"https://www.imdb.com/title/{imdb_id}/"
        }

    except:
        return None
