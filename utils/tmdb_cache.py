import sqlite3
from config import TMDB_CACHE_DB


# -------------------------------------------------
# INIT DB
# -------------------------------------------------

def init_tmdb_cache():

    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS tmdb_cache (
            title TEXT PRIMARY KEY,
            poster TEXT,
            year TEXT,
            genre TEXT,
            rating TEXT
        )
    """)

    conn.commit()
    conn.close()


# run once on import
init_tmdb_cache()


# -------------------------------------------------
# GET CACHE
# -------------------------------------------------

def get_cached_tmdb(title):

    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    c.execute(
        "SELECT poster, year, genre, rating FROM tmdb_cache WHERE title=?",
        (title.lower(),)
    )

    row = c.fetchone()
    conn.close()

    return row


# -------------------------------------------------
# SAVE CACHE
# -------------------------------------------------

def save_cached_tmdb(title, poster, year, genre, rating):

    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    c.execute("""
        INSERT OR REPLACE INTO tmdb_cache
        (title, poster, year, genre, rating)
        VALUES (?, ?, ?, ?, ?)
    """, (
        title.lower(),
        poster,
        year,
        genre,
        rating
    ))

    conn.commit()
    conn.close()
