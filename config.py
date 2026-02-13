import logging
from logging.handlers import RotatingFileHandler
from pymongo import TEXT 

# --- Bot Configuration ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "7887669088:AAES-gPD9vRFT-WOvzWSM__eBZrhLnaD7r8"
MONGO_URI = "mongodb+srv://ss:ss@seriesfilterbot.p88od4y.mongodb.net/?appName=SeriesFilterBOT"

# --- Indexing Configuration ---
# default fallback only. Actual list is stored in MongoDB and updated via bot commands.
INDEX_CHANNELS = []  

ADMIN_USERS = [5794562450]
FORCE_SUB_CHANNEL = None   
LOG_FILE_PATH = "bot.log" # Path remains relative, but the handler will create it

# Images
START_IMG = "https://ibb.co/1tSFHQKw"
SPELL_IMG = "https://ibb.co/1tSFHQKw"
# Poster / OMDB Config
OMDB_API_KEY = "f93ab313"
OMDB_CACHE_DB = "/tmp/omdb_cache.db"
DEFAULT_POSTER = "https://ibb.co/1tSFHQKw"
MAX_CALLBACK_LENGTH = 50

# Captions
CUSTOM_CAPTION_TEXT = """
`{file_name}`

╔════ ᴊᴏɪɴ ᴡɪᴛʜ ᴜs ═════╗
◻️ 𝙅𝙊𝙄𝙉 :- @SeriesXSpace
◻️ 𝙅𝙊𝙄𝙉 :- @SeriesXSpaceChannel
◻️ 𝙅𝙊𝙄𝙉 :- @TGxSeriesFilterBot
╚════ ᴊᴏɪɴ ᴡɪᴛʜ ᴜs ═════╝"""

LOG_FILE_PATH = "bot.log"
MAX_LOG_SIZE = 10 * 1024 * 1024

formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

file_handler = RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=MAX_LOG_SIZE,
    backupCount=5,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger("BOT")
logger.info("Logger initialized")
