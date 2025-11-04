# movie_fetcher.py
# (This file replaces imdb.py to avoid a circular import)

# Ensure you have installed this: pip install cinemagoer

from imdb import IMDb

ia = IMDb()

def get_imdb_data(query: str):
    try:
        res = ia.search_movie(query)              # better than search_for_title

        if not res:
            return None

        mid = res[0].movieID                      # first result id
        m = ia.get_movie(mid)                     # FULL fetch

        title  = m.get('title') or query

        poster = (
            m.get('full-size cover url')
            or m.get('full-size poster')
            or m.get('cover url')
            or m.get('poster')
        )

        link = f"https://www.imdb.com/title/tt{mid}/"

        # if still None → return None OR empty string
        return {
            "title": title,
            "poster": poster or "",
            "link": link
        }

    except Exception as e:
        print("cinemagoer fail:", e)
        return None

