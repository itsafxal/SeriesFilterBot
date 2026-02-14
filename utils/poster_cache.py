import sqlite3
from config import TMDB_CACHE_DB


def init_cache():
    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS poster_cache (
            title TEXT PRIMARY KEY,
            poster TEXT,
            year TEXT,
            genre TEXT,
            rating TEXT,
            source TEXT
        )
    """)

    conn.commit()
    conn.close()


init_cache()


# -------------------------------------------------
# GET CACHE
# -------------------------------------------------

def get_cached(title):

    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    c.execute("""
        SELECT poster, year, genre, rating, source
        FROM poster_cache
        WHERE title=?
    """, (title.lower(),))

    row = c.fetchone()
    conn.close()

    return row


# -------------------------------------------------
# SAVE CACHE (TMDB OR OMDB)
# -------------------------------------------------

def save_cache(title, poster, year, genre, rating, source):

    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    c.execute("""
        INSERT OR REPLACE INTO poster_cache
        (title, poster, year, genre, rating, source)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        title.lower(),
        poster,
        year,
        genre,
        rating,
        source
    ))

    conn.commit()
    conn.close()
