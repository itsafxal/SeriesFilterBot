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

ADMIN_USERS = [5483128891]
FORCE_SUB_CHANNEL = None   
LOG_FILE_PATH = "bot.log" # Path remains relative, but the handler will create it

# Images
START_IMG = "https://ibb.co/1tSFHQKw"
SPELL_IMG = "https://ibb.co/1tSFHQKw"
# Poster / OMDB Config
OMDB_API_KEY = "f93ab313"
OMDB_CACHE_DB = "/tmp/omdb_cache.db"
DEFAULT_POSTER = "https://ibb.co/1tSFHQKw"

# Captions
CUSTOM_CAPTION_TEXT = """
`{file_name}`

╔════ ᴊᴏɪɴ ᴡɪᴛʜ ᴜs ═════╗
◻️ 𝙅𝙊𝙄𝙉 :- @SeriesXSpace
◻️ 𝙅𝙊𝙄𝙉 :- @SeriesXSpaceChannel
◻️ 𝙅𝙊𝙄𝙉 :- @TGxSeriesFilterBot
╚════ ᴊᴏɪɴ ᴡɪᴛʜ ᴜs ═════╝"""

MAX_CALLBACK_LENGTH = 50
MAX_LOG_SIZE = 10 * 1024 * 1024 

file_handler = RotatingFileHandler(
    LOG_FILE_PATH, 
    maxBytes=MAX_LOG_SIZE, 
    backupCount=5, 
    encoding='utf-8'
)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

# Configure the root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler) # <-- Explicitly attach to root

# Configure the 'config' logger 
logger = logging.getLogger("config")
logger.info("Starting bot process...")
