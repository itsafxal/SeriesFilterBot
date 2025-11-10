# imdb.py 
import requests
from typing import Optional, Dict, Any, Union, Tuple
from config import OMDB_API_KEY
import logging

logger = logging.getLogger(__name__)

# New function to fetch image content safely
def _fetch_image_content(url: str, timeout: int = 5) -> Optional[bytes]:
    if not url or url == 'N/A':
        return None
    
    # Ensure URL is HTTPS for best compatibility (already handled in fetch_omdb, but safe to check)
    if url.startswith("http://"):
        url = url.replace("http://", "https://")

    try:
        # Request with a small timeout and stream=True to handle large files
        r = requests.get(url, timeout=timeout, stream=True, allow_redirects=True)
        r.raise_for_status()
        
        # Check content type to ensure it's an image
        if 'image' not in r.headers.get('Content-Type', '').lower():
            logger.warning(f"OMDb returned non-image content for poster URL: {url}")
            return None
        
        # Read content (adjust max_size if needed, e.g., 5MB limit for photos)
        MAX_SIZE = 5 * 1024 * 1024 # 5 MB
        content = b''
        for chunk in r.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > MAX_SIZE:
                logger.warning(f"OMDb poster too large (>{MAX_SIZE} bytes): {url}")
                return None
        
        return content
    
    except requests.RequestException as e:
        logger.error(f"Failed to fetch poster content from {url}: {e}")
        return None

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
            # Pass original error if possible
            error_message = s.get('Error', 'Movie not found')
            return {"error": error_message} 

        if not s.get("Search"):
            return {"error": "Movie not found"}
        
        imdb_id = s["Search"][0]["imdbID"]
        data = _request({"i": imdb_id, "apikey": OMDB_API_KEY})

    # --- IMAGE FETCHING LOGIC ---
    poster_url = data.get("Poster")
    poster_content = None
    
    if poster_url and poster_url not in ('N/A', ''):
        # Fetch the actual image content instead of relying on the URL
        poster_content = _fetch_image_content(poster_url)
    
    # --- END IMAGE FETCHING LOGIC ---

    return {
        "title": data.get("Title"),
        "year": data.get("Year"),
        "rating": data.get("imdbRating"),
        "genres": (data.get("Genre") or "").split(", ") if data.get("Genre") else [],
        "plot": data.get("Plot"),
        # Return content bytes instead of URL
        "poster_content": poster_content, 
        "imdb_id": data.get("imdbID")
    }


# backward compatibility for your old code — you can delete get_imdb_data calls slowly
def get_imdb_data(query: str) -> Dict[str, Union[str, bytes, list, None]]:
    try:
        return fetch_omdb(query)
    except Exception as e:
        logger.error(f"Top level OMDb fetch failed: {e}")
        # Return structured error dictionary to handle gracefully
        return {"error": str(e)}
