# database/sql.py
import aiosqlite
from async_lru import alru_cache

class SQLDB:
    def __init__(self):
        self.db_path = "search.db"
        self.conn = None

    async def init(self):
        self.conn = await aiosqlite.connect(self.db_path)

        # Main table
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                file_name TEXT,
                file_link TEXT,
                unique_id TEXT PRIMARY KEY,
                caption TEXT,
                file_id TEXT,    
                mime_type TEXT  
            )
        """)

        # FTS5 table
        await self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts 
            USING fts5(file_name, caption, unique_id UNINDEXED)
        """)

        await self.conn.commit()

    async def insert_bulk(self, files_list):
        """
        files_list structure: (file_name, file_link, unique_id, caption, file_id, mime_type)
        """
        if not files_list:
            return

        # Insert or replace in main table
        await self.conn.executemany(
            "INSERT OR REPLACE INTO files (file_name, file_link, unique_id, caption, file_id, mime_type) VALUES (?, ?, ?, ?, ?, ?)",
            files_list
        )

        # Insert into FTS table: (file_name, caption, unique_id)
        await self.conn.executemany(
            "INSERT OR REPLACE INTO files_fts (file_name, caption, unique_id) VALUES (?, ?, ?)",
            [(x[0], x[3], x[2]) for x in files_list]
        )

        await self.conn.commit()

    # =========================================
    # Fetch single record by unique_id
    # =========================================
    async def get_by_id(self, unique_id):
        async with self.conn.execute(
            "SELECT file_name, file_link, unique_id, caption, file_id, mime_type FROM files WHERE unique_id = ? LIMIT 1",
            (unique_id,)
        ) as cursor:
            return await cursor.fetchone()

    # =========================================
    # Search function (FTS5 + LRU Cache)
    # =========================================
    @alru_cache(maxsize=2048)
    async def search(self, query):
        """
        LRU cached search (instant repeat queries).
        Uses FTS5 for speed + fallback LIKE.
        """

        q = f"%{query}%"

        try:
            # Try FTS first (fastest)
            async with self.conn.execute(
                """
                SELECT unique_id, file_name, file_link, caption, file_id, mime_type
                FROM files
                WHERE rowid IN (
                    SELECT rowid FROM files_fts WHERE files_fts MATCH ?
                )
                LIMIT 50
                """,
                (query,)
            ) as cursor:
                results = await cursor.fetchall()
                if results:
                    return results
        except:
            pass  # FTS may fail (special characters)

        # Fallback LIKE search
        async with self.conn.execute(
            """
            SELECT unique_id, file_name, file_link, caption, file_id, mime_type
            FROM files
            WHERE file_name LIKE ?
            LIMIT 50
            """,
            (q,)
        ) as cursor:
            return await cursor.fetchall()


sql_db = SQLDB()
