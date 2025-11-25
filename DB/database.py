# database/mongo.py
import re
import datetime
import pytz
from motor.motor_asyncio import AsyncIOMotorClient
from config import Config

class MongoDB:
    def __init__(self):
        self.client = AsyncIOMotorClient(Config.MONGO_URI)
        self.db = self.client[Config.MONGO_DB_NAME]
        
        # Collections
        self.col = self.db["files"] 
        self.user_col = self.db["users"]
        self.group_col = self.db["groups"]
        self.settings_col = self.db["settings"]

    # --------------------------------------------------------
    # PROCESS FILENAME
    # --------------------------------------------------------

    def _process_file_name(self, file_name: str, caption: str | None):
        """
        Applies filename cleaning:
        1. Uses caption if available
        2. Detects SxxExx patterns
        3. Removes watermarks / Telegram tags
        """
        raw_name = caption or file_name or "N/A"

        # ---------------------------
        # Season / Episode Detection
        # ---------------------------

        season_pattern = re.compile(
            r'(?<!\w)(?:s|season)[\s._-]*?(\d{1,2})[\s._-]*?(?:e|ep|episode)[\s._-]*?(\d{1,2})(?!\d)',
            re.IGNORECASE
        )
        match = season_pattern.search(raw_name)

        prefix = ""
        if match:
            s = int(match.group(1))
            e = int(match.group(2))
            prefix = f"[S{s:02d} EP{e:02d}] "

            # remove original SxxExx from string
            raw_name = season_pattern.sub("", raw_name)

        # ---------------------------
        # Watermark Removal
        # ---------------------------

        removal_pattern = re.compile(
            r"(_|\+\s|\-|\.|\+|\[MM\]\s|\[MM\]_|\@TvSeriesBay|\@Cinema\sCompany|"
            r"\@Cinema_Company|\@CC_|\@CC|\@MM_New|\@MM_Linkz|\@MOVIEHUNT|\@CL|"
            r"\@FBM|\@CKMSERIES|www_DVDWap_Com_|MLM|\@WMR|\[CF\]\s|\[CF\]|"
            r"\@IndianMoviez|\@tamil_mm|\@infotainmentmedia|\@trolldcompany|"
            r"\@Rarefilms|\@yamandanmovies|\[YM\]|\@Mallu_Movies|\@YTSLT|"
            r"\@DailyMovieZhunt|\@I_M_D_B|\@CC_All|\@PM_Old|Dvdworld|\[KMH\]|"
            r"\@FBM_HW|\@Film_Kottaka|\@CC_X265|\@CelluloidCineClub|"
            r"\@cinemaheist|\@telugu_moviez|\@CR_Rockers|\@CCineClub|KC_|"
            r"\[KC\]|\[AML\])",
            re.IGNORECASE
        )

        cleaned_name = removal_pattern.sub(" ", raw_name)
        cleaned_name = re.sub(r'\s{2,}', ' ', cleaned_name).strip()

        return f"{prefix}{cleaned_name}".strip()

    # --------------------------------------------------------
    # FILE MANAGEMENT (Upsert)
    # --------------------------------------------------------

    async def add_file(self, media: dict):
        """Saves file metadata to the 'files' collection."""
        processed_name = self._process_file_name(
            file_name=media.get("file_name"),
            caption=media.get("caption")
        )

        document = {
            "unique_id": media["unique_id"],
            "file_id": media["file_id"],
            "file_name": processed_name,
            "file_size": media.get("file_size", 0),
            "mime_type": media.get("mime_type"),
            "caption": media.get("caption") or processed_name,
            "chat_id": media.get("chat_id"),
            "msg_id": media.get("msg_id"),
            "updated_at": datetime.datetime.now(pytz.utc)
        }

        await self.col.update_one(
            {"unique_id": media["unique_id"]},
            {"$set": document},
            upsert=True
        )

    # --------------------------------------------------------
    # FILE FETCH / COUNT
    # --------------------------------------------------------

    async def get_file_by_unique_id(self, unique_id: str):
        """Fetches a single file document from Mongo by unique_id."""
        return await self.col.find_one({"unique_id": unique_id})

    async def get_all_files(self):
        cursor = self.col.find({})
        async for doc in cursor:
            yield doc

    async def count_files(self):
        return await self.col.count_documents({})
        
    # ========================================================
    # USER MANAGEMENT METHODS (New)
    # ========================================================
    async def add_user(self, user_id: int, username: str | None = None):
        """Adds or updates a user in the 'users' collection."""
        user_data = {
            "user_id": user_id,
            "username": username,
            "join_date": datetime.datetime.now(pytz.utc),
            "is_blocked": False
        }
        await self.user_col.update_one(
            {"user_id": user_id},
            {"$set": user_data},
            upsert=True
        )
        
    async def get_all_users(self):
        """Returns a list of all user IDs."""
        cursor = self.user_col.find({}, {"user_id": 1})
        return [doc["user_id"] async for doc in cursor]

    async def count_users(self):
        """Returns the total number of saved users."""
        return await self.user_col.count_documents({})

    # ========================================================
    # GROUP MANAGEMENT METHODS (New)
    # ========================================================
    async def add_group(self, chat_id: int, title: str):
        """Adds or updates a group in the 'groups' collection."""
        group_data = {
            "chat_id": chat_id,
            "title": title,
            "join_date": datetime.datetime.now(pytz.utc),
            "is_blocked": False
        }
        await self.group_col.update_one(
            {"chat_id": chat_id},
            {"$set": group_data},
            upsert=True
        )

    async def get_all_groups(self):
        """Returns a list of all group IDs."""
        cursor = self.group_col.find({}, {"chat_id": 1})
        return [doc["chat_id"] async for doc in cursor]
        
    async def count_groups(self):
        """Returns the total number of saved groups."""
        return await self.group_col.count_documents({})

    # ========================================================
    # GLOBAL SETTINGS METHODS (New)
    # ========================================================
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Retrieves a global setting value."""
        doc = await self.settings_col.find_one({"_id": key})
        return doc["value"] if doc else default

    async def set_setting(self, key: str, value: str):
        """Sets or updates a global setting value."""
        await self.settings_col.update_one(
            {"_id": key},
            {"$set": {"value": value, "updated_at": datetime.datetime.now(pytz.utc)}},
            upsert=True
        )


mongo_db = MongoDB()
