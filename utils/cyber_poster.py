import sqlite3
from config import TMDB_CACHE_DB


# -------------------------------------------------
# INIT CACHE DB
# -------------------------------------------------

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
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


# initialize once
init_cache()


# -------------------------------------------------
# GET CACHE
# -------------------------------------------------

def get_cached(title: str):

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
# SAVE CACHE
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


# -------------------------------------------------
# OPTIONAL: CLEAR CACHE
# -------------------------------------------------

def clear_cache():

    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    c.execute("DELETE FROM poster_cache")

    conn.commit()
    conn.close()
