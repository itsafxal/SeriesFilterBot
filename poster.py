# imdb.py

# git+https://github.com/Joelkb/cinemagoer

# Removed: import requests
# Removed: from bs4 import BeautifulSoup
from imdb import IMDb # Import the official cinemagoer library

# Initialize the cinemagoer interface outside the function for performance
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
            
        # Get the top result (usually the correct one)
        top_result = search_results[0]
        
        # Fetch the complete data for the movie/series
        ia.update(top_result, ['main', 'plot'])
        
        # Construct the necessary return data
        title = top_result.get('title')
        # Get the URL for the poster (uses 'full-size' image if available)
        poster = top_result.get('full-size poster') or top_result.get('cover url')
        # Construct the official IMDB link
        link = f"https://www.imdb.com/title/tt{top_result.movieID}/"
        
        if not poster:
             # Fallback if poster is missing, but data found
            return {"title": title, "poster": None, "link": link}

        return {"title": title, "poster": poster, "link": link}

    except Exception as e:
        # Log the error if the library fails to fetch data
        # In a real bot, you'd log this with your config.logger
        print(f"IMDB data fetch failed using cinemagoer for '{query}': {e}")
        return None
