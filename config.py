import logging
from pymongo import TEXT 

# --- Bot Configuration ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "7661197159:AAHgvfHL2qK-r-7uDiNkAjROGL1nYjM6cZE"
MONGO_URI = "mongodb+srv://ss:ss@seriesfilterbot.p88od4y.mongodb.net/?appName=SeriesFilterBOT"

# --- Indexing Configuration ---
# default fallback only. Actual list is stored in MongoDB and updated via bot commands.
INDEX_CHANNELS = []  

ADMIN_USERS = [5483128891]
FORCE_SUB_CHANNEL = None   

# Images
START_IMG = "https://sanishtech.com/i/690cfef2312bd-1762459378.jpg"

# Poster / OMDB Config
OMDB_API_KEY = "f93ab313"
OMDB_CACHE_DB = "/tmp/omdb_cache.db"
DEFAULT_POSTER = "https://sanishtech.com/i/690ceab460363-1762454196.jpg"

# Captions
CUSTOM_CAPTION_TEXT = """
`{file_name}`

🔗 **Please Use Our Bot **
@{bot_username}
"""
MAX_CALLBACK_LENGTH = 50

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("config")
