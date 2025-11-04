import requests
from bs4 import BeautifulSoup

def get_imdb_data(query):
    """
    Fetches IMDB data (title, poster URL, link) for a given query.
    Note: This is a synchronous function and should be run in an executor 
    in an async environment like Pyrogram.
    """
    search_url = f"https://www.imdb.com/find?q={query.replace(' ', '+')}&s=tt"
    r = requests.get(search_url)
    soup = BeautifulSoup(r.text, "html.parser")
    # Tries to find the first search result link
    result = soup.select_one(".findResult a")
    if not result:
        return None
        
    link = "https://www.imdb.com" + result.get("href")
    page = requests.get(link)
    psoup = BeautifulSoup(page.text, "html.parser")
    
    # Extract Title (h1)
    title_element = psoup.find("h1")
    title = title_element.text.strip() if title_element else "Unknown Title"
    
    # Extract Poster (using the common ipc-image selector)
    poster_element = psoup.select_one(".ipc-image")
    poster = poster_element["src"] if poster_element and "src" in poster_element.attrs else None
    
    return {"title": title, "poster": poster, "link": link}
