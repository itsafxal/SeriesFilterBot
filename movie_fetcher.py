from imdb import IMDb

ia = IMDb()

def get_imdb_data(query: str):
    try:
        res = ia.search_movie(query)

        if not res:
            return None

        mid = res[0].movieID
        m = ia.get_movie(mid)

        title  = m.get('title') or query

        # NEW SAFE POSTER EXTRACT
        poster = None
        for key in [
            'full-size cover url',
            'cover url',
            'full-size poster',
            'poster'
        ]:
            val = m.get(key)
            if val:
                poster = val
                break

        link = f"https://www.imdb.com/title/tt{mid}/"

        return {
            "title": title,
            "poster": poster or "",
            "link": link
        }

    except Exception as e:
        print("cinemagoer fail:", e)
        return None
