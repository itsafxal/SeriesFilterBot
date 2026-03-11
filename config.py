import logging
from logging.handlers import RotatingFileHandler
from pymongo import TEXT

# BOT SETTINGS
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "8333630020:AAE4ZBaAjzU_Qs5CyxHpkLs6vwn1jJxoXco"

# MONGO DB
MONGO_URI_CONFIG = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?appName=Cluster0"
MONGO_URI_DB1    = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?appName=Cluster0"
MONGO_URI_DB2    = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?appName=Cluster0"

# ADMIN ID
ADMIN_USERS = [5794562450]

# IMAGES
START_IMG      = "https://files.catbox.moe/ivj3km.jpg"
SPELL_IMG      = "https://files.catbox.moe/krlzjs.jpg"
DEFAULT_POSTER = "https://files.catbox.moe/rjj4do.jpg"

# POSTER API
import random as _random

# ── Dual API keys — randomly rotated to double rate limits ──────
# Add your 2nd key in index [1] for each pool
TMDB_API_KEYS = [
    "0e027f75bc7f6a14139ad3e6fe76ed97",   # TMDB key 1
    "0e027f75bc7f6a14139ad3e6fe76ed97",   # TMDB key 2  ← replace with 2nd key
]
OMDB_API_KEYS = [
    "f93ab313",   # OMDB key 1
    "fd7c2997",   # OMDB key 2  ← replace with 2nd key
]

def TMDB_API_KEY() -> str:
    """Returns a random TMDB key from the pool on every call."""
    return _random.choice(TMDB_API_KEYS)

def OMDB_API_KEY() -> str:
    """Returns a random OMDB key from the pool on every call."""
    return _random.choice(OMDB_API_KEYS)
TMDB_CACHE_DB         = "/tmp/TMDB_cache.db"
TMDB_IMAGE_BASE       = "https://image.tmdb.org/t/p/w500"       # portrait (default)
TMDB_IMAGE_BASE_WIDE  = "https://image.tmdb.org/t/p/w780"       # landscape / wide

# ── Poster defaults (admin can override via /poster command) ──
POSTER_ENABLED   = True    # show/hide poster
POSTER_LANDSCAPE = False   # False = portrait (w500), True = landscape (w780)

# ── Caption cleaning defaults ──────────────────────────────────
# Keywords removed from file_name_clean / caption_clean at save time.
# Admin can add/remove via /captionclean command.
DEFAULT_CAPTION_CLEAN_KEYWORDS = [
    "@", "t.me", "Telegram", "www.", "http://", "https://",
]


# ── Sticker & progress settings ────────────────────────────────
STICKER_ENABLED          = True   # season start/end stickers
PROGRESS_MESSAGE_ENABLED = True   # "Sending files... X/Y" — deleted when done

# ── Filter default ─────────────────────────────────────────────
FILTER_ENABLED_DEFAULT = True   # global filter on/off default

# AS YOUR WISH
INDEX_BATCH_SIZE   = 100          # files per dedup/save batch

INDEX_CHANNELS     = []
MAX_CALLBACK_LENGTH = 50
SEASONS_PER_PAGE   = 9
FORCE_SUB_CHANNEL  = None
LOG_FILE_PATH      = "bot.log"
MAX_LOG_SIZE       = 10 * 1024 * 1024

# LOGGING INFO
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

# ✅ Silence APScheduler logs
for name in [
    "apscheduler",
    "apscheduler.scheduler",
    "apscheduler.executors",
    "apscheduler.executors.default",
    "apscheduler.jobstores",
]:
    logging.getLogger(name).setLevel(logging.CRITICAL)
