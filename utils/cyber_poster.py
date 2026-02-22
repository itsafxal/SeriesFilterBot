import sqlite3
from config import TMDB_CACHE_DB


# -------------------------------------------------
# INIT CACHE DB (WITH LANGUAGE COLUMN)
# -------------------------------------------------

def init_cache():

    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    # Create table if not exists
    c.execute("""
        CREATE TABLE IF NOT EXISTS poster_cache (
            title TEXT PRIMARY KEY,
            poster TEXT,
            year TEXT,
            genre TEXT,
            rating TEXT,
            source TEXT,
            language TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 🔥 Auto-migration: add language column if old DB
    c.execute("PRAGMA table_info(poster_cache)")
    columns = [col[1] for col in c.fetchall()]

    if "language" not in columns:
        c.execute("ALTER TABLE poster_cache ADD COLUMN language TEXT")

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
        SELECT poster, year, genre, rating, source, language
        FROM poster_cache
        WHERE title=?
    """, (title.lower(),))

    row = c.fetchone()
    conn.close()

    return row


# -------------------------------------------------
# SAVE CACHE
# -------------------------------------------------

def save_cache(title, poster, year, genre, rating, source, language):

    conn = sqlite3.connect(TMDB_CACHE_DB)
    c = conn.cursor()

    c.execute("""
        INSERT OR REPLACE INTO poster_cache
        (title, poster, year, genre, rating, source, language)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        title.lower(),
        poster,
        year,
        genre,
        rating,
        source,
        language
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
