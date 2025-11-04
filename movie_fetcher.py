from imdb import IMDb

ia = IMDb()

def get_imdb_data(query: str):
    try:
        res = ia.search_movie(query)
        if not res:
            return None

        mid = res[0].movieID
        m = ia.get_movie(mid)

        poster = None
        for key in [
            "full-size cover url",
            "cover url",
            "full-size poster",
            "poster"
        ]:
            val = m.get(key)
            if val:
                poster = val
                break

        # fix http → https
        if poster and poster.startswith("http://"):
            poster = poster.replace("http://", "https://")

        link = f"https://www.imdb.com/title/tt{mid}/"

        return {
            "title": m.get("title") or query,
            "poster": poster or "",
            "link": link
        }

    except Exception:
        return None
