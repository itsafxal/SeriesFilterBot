import logging
import random as _random
from logging.handlers import RotatingFileHandler

# BOT SETTINGS
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "8231385133:AAF3eGcM5wev6tt8JJ0LisknyxxoutkUk_s"

# MONGO DB
MONGO_URI_CONFIG = "mongodb+srv://ss:ss@seriesfilterbot.p88od4y.mongodb.net/?appName=SeriesFilterBOT"
MONGO_URI_DB1    = "mongodb+srv://SB1:SB1@cluster0.foxpowr.mongodb.net/?appName=Cluster0"
MONGO_URI_DB2    = "mongodb+srv://SB2:SB2@cluster0.04scp0s.mongodb.net/?appName=Cluster0"

# ADMIN ID
ADMIN_USERS = [5794562450]

# IMAGES
START_IMG      = "https://files.catbox.moe/ivj3km.jpg"
SPELL_IMG      = "https://files.catbox.moe/krlzjs.jpg"
DEFAULT_POSTER = "https://files.catbox.moe/rjj4do.jpg"

# POSTER API
# Dual API keys — randomly rotated to double rate limits
TMDB_API_KEYS = [
    "0e027f75bc7f6a14139ad3e6fe76ed97",   # TMDB key 1
    "d13fa0ad942cf34ce943a82c95dc3e92",   # TMDB key 2
]
OMDB_API_KEYS = [
    "f93ab313",   # OMDB key 1
    "fd7c2997",   # OMDB key 2
]

def TMDB_API_KEY() -> str:
    return _random.choice(TMDB_API_KEYS)

def OMDB_API_KEY() -> str:
    return _random.choice(OMDB_API_KEYS)

TMDB_CACHE_DB        = "/tmp/TMDB_cache.db"
TMDB_IMAGE_BASE      = "https://image.tmdb.org/t/p/w500"
TMDB_IMAGE_BASE_WIDE = "https://image.tmdb.org/t/p/w780"

POSTER_ENABLED   = True
POSTER_LANDSCAPE = False

DEFAULT_CAPTION_CLEAN_KEYWORDS = [
    "@", "t.me", "Telegram", "www.", "http://", "https://",
]

STICKER_ENABLED          = True
PROGRESS_MESSAGE_ENABLED = True
FILTER_ENABLED_DEFAULT   = True

INDEX_BATCH_SIZE    = 100
INDEX_CHANNELS      = []
MAX_CALLBACK_LENGTH = 50
SEASONS_PER_PAGE    = 9
FORCE_SUB_CHANNEL   = None
LOG_FILE_PATH       = "bot.log"
MAX_LOG_SIZE        = 10 * 1024 * 1024

# LOGGING
formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

file_handler = RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=MAX_LOG_SIZE,
    backupCount=5,
    encoding="utf-8",
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

# Silence APScheduler logs
for _name in [
    "apscheduler",
    "apscheduler.scheduler",
    "apscheduler.executors",
    "apscheduler.executors.default",
    "apscheduler.jobstores",
]:
    logging.getLogger(_name).setLevel(logging.CRITICAL)
