# movie_fetcher.py
# (This file replaces imdb.py to avoid a circular import)

# Ensure you have installed this: pip install cinemagoer

from imdb import IMDb 

# Initialize the cinemagoer interface once
ia = IMDb()

def get_imdb_data(query):
    """
    Fetches IMDB data (title, poster URL, link) using the cinemagoer library.
    """
    try:
        # Search for the series/title
        search_results = ia.search_for_title(query)
        
        if not search_results:
            return None
            
        # Get the top result
        top_result = search_results[0]
        
        # Fetch the complete data for the movie/series
        ia.update(top_result, ['main', 'plot'])
        
        title = top_result.get('title')
        # Prioritize 'full-size poster', fallback to 'cover url'
        poster = top_result.get('full-size poster') or top_result.get('cover url')
        link = f"https://www.imdb.com/title/tt{top_result.movieID}/"
        
        return {"title": title, "poster": poster, "link": link}

    except Exception as e:
        # Logging here helps confirm if cinemagoer failed to find the data
        print(f"IMDB data fetch failed using cinemagoer for '{query}': {e}")
        return None
