from imdb import IMDb

ia = IMDb()

def get_imdb_data(query: str):
    try:
        results = ia.search_movie(query)
        if not results:
            return None

        mid = results[0].movieID
        m = ia.get_movie(mid)

        # better poster resolve
        poster = None

        if 'full-size cover url' in m:
            poster = m['full-size cover url']
        elif 'cover url' in m:
            poster = m['cover url']
        elif 'full-size poster' in m:
            poster = m['full-size poster']
        elif 'poster' in m:
            poster = m['poster']

        # NEW – fallback to moviePhoto url (this works 95% cases)
        if not poster and 'movie photo' in m:
            poster = m['movie photo']

        if poster and poster.startswith("http://"):
            poster = poster.replace("http://", "https://")

        return {
            "title": m.get("title", query),
            "year": m.get("year", "?"),
            "genres": ", ".join(m.get("genres", [])),
            "rating": m.get("rating", "?"),
            "poster": poster,
            "link": f"https://www.imdb.com/title/tt{mid}/"
        }

    except Exception:
        return None
