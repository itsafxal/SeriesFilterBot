import logging
from logging.handlers import RotatingFileHandler
from pymongo import TEXT 

# BOT SETTINGS
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "7887669088:AAES-gPD9vRFT-WOvzWSM__eBZrhLnaD7r8"
MONGO_URI = "mongodb+srv://ss:ss@seriesfilterbot.p88od4y.mongodb.net/?appName=SeriesFilterBOT"

# ADMIN ID
ADMIN_USERS = [5794562450]

# IMAGES
START_IMG = "https://ibb.co/1tSFHQKw"
SPELL_IMG = "https://ibb.co/1tSFHQKw"
DEFAULT_POSTER = "https://ibb.co/1tSFHQKw"

# POSTER API
TMDB_API_KEY = "0e027f75bc7f6a14139ad3e6fe76ed97"
OMDB_API_KEY = "f93ab313"
TMDB_CACHE_DB = "/tmp/TMDB_cache.db"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# Captions
CUSTOM_CAPTION_TEXT = """
`{file_name}`

╔════ ᴊᴏɪɴ ᴡɪᴛʜ ᴜs ═════╗
◻️ 𝙅𝙊𝙄𝙉 :- @SeriesXSpace
◻️ 𝙅𝙊𝙄𝙉 :- @SeriesXSpaceChannel
◻️ 𝙅𝙊𝙄𝙉 :- @TGxSeriesFilterBot
╚════ ᴊᴏɪɴ ᴡɪᴛʜ ᴜs ═════╝"""

# AS YOUR WISH
INDEX_CHANNELS = []  
MAX_CALLBACK_LENGTH = 50
SEASONS_PER_PAGE = 9
FORCE_SUB_CHANNEL = None   
LOG_FILE_PATH = "bot.log"
MAX_LOG_SIZE = 10 * 1024 * 1024

#LOGGING INFO 
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
