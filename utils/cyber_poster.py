"""
cyber_poster.py — FIXED & OPTIMIZED
=====================================
FIXES:
  1. Thread-local connection pool — no open/close per call
  2. WAL mode enabled at init
  3. Language column auto-migration preserved
"""

import sqlite3
import threading
from config import TMDB_CACHE_DB

# ── Thread-local connection ───────────────────────────────────
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(TMDB_CACHE_DB, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")   # 8MB page cache
        _local.conn = conn
    return conn


def init_cache():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poster_cache (
            title      TEXT PRIMARY KEY,
            poster     TEXT,
            year       TEXT,
            genre      TEXT,
            rating     TEXT,
            source     TEXT,
            language   TEXT,
            plot       TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Auto-migration: add language column if old DB
    existing = {row[1] for row in conn.execute("PRAGMA table_info(poster_cache)").fetchall()}
    if "language" not in existing:
        conn.execute("ALTER TABLE poster_cache ADD COLUMN language TEXT")
    if "plot" not in existing:
        conn.execute("ALTER TABLE poster_cache ADD COLUMN plot TEXT")
    conn.commit()


init_cache()


def get_cached(title: str):
    conn   = _get_conn()
    cursor = conn.execute("""
        SELECT poster, year, genre, rating, source, language, plot
        FROM poster_cache WHERE title=?
    """, (title.lower(),))
    return cursor.fetchone()


def save_cache(title, poster, year, genre, rating, source, language, plot=""):
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO poster_cache
        (title, poster, year, genre, rating, source, language, plot)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (title.lower(), poster, year, genre, rating, source, language, plot))
    conn.commit()


def clear_cache():
    conn = _get_conn()
    conn.execute("DELETE FROM poster_cache")
    conn.commit()
