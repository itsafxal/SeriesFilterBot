import logging
import os
from logging.handlers import RotatingFileHandler
from itertools import cycle
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# =========================
# BOT SETTINGS
# =========================
API_ID = int(os.getenv("API_ID", "28643132"))
API_HASH = os.getenv("API_HASH", "510c3009ea589830a89e045bf8aae656")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8231385133:AAF3eGcM5wev6tt8JJ0LisknyxxoutkUk_s")

# =========================
# MONGO DB
# =========================
MONGO_URI_CONFIG = os.getenv(
    "MONGO_URI_CONFIG",
    "mongodb+srv://ss:ss@seriesfilterbot.p88od4y.mongodb.net/?appName=SeriesFilterBOT"
)
MONGO_URI_DB1 = os.getenv(
    "MONGO_URI_DB1",
    "mongodb+srv://SB1:SB1@cluster0.foxpowr.mongodb.net/?appName=Cluster0"
)
MONGO_URI_DB2 = os.getenv(
    "MONGO_URI_DB2",
    "mongodb+srv://SB2:SB2@cluster0.04scp0s.mongodb.net/?appName=Cluster0"
)

# =========================
# ADMIN
# =========================
ADMIN_USERS = list(map(int, os.getenv("ADMIN_USERS", "5794562450").split(",")))

# =========================
# IMAGES
# =========================
START_IMG = os.getenv("START_IMG", "https://files.catbox.moe/ivj3km.jpg")
SPELL_IMG = os.getenv("SPELL_IMG", "https://files.catbox.moe/krlzjs.jpg")
DEFAULT_POSTER = os.getenv("DEFAULT_POSTER", "https://files.catbox.moe/rjj4do.jpg")

# =========================
# POSTER API KEYS
# =========================
TMDB_API_KEYS = [k.strip() for k in os.getenv(
    "TMDB_API_KEYS",
    "0e027f75bc7f6a14139ad3e6fe76ed97,d13fa0ad942cf34ce943a82c95dc3e92"
).split(",") if k.strip()]

OMDB_API_KEYS = [k.strip() for k in os.getenv(
    "OMDB_API_KEYS",
    "f93ab313,fd7c2997"
).split(",") if k.strip()]

# Create rotating iterators (IMPORTANT: define once)
_tmdb_cycle = cycle(TMDB_API_KEYS)
_omdb_cycle = cycle(OMDB_API_KEYS)

def TMDB_API_KEY() -> str:
    """Return next TMDB API key (rotating)"""
    return next(_tmdb_cycle)

def OMDB_API_KEY() -> str:
    """Return next OMDB API key (rotating)"""
    return next(_omdb_cycle)

# =========================
# TMDB CONFIG
# =========================
TMDB_CACHE_DB = "/tmp/TMDB_cache.db"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
TMDB_IMAGE_BASE_WIDE = "https://image.tmdb.org/t/p/w780"

POSTER_ENABLED = os.getenv("POSTER_ENABLED", "True").lower() == "true"
POSTER_LANDSCAPE = os.getenv("POSTER_LANDSCAPE", "False").lower() == "true"

# =========================
# CAPTION CLEANUP
# =========================
DEFAULT_CAPTION_CLEAN_KEYWORDS = [
    "@", "t.me", "Telegram", "www.", "http://", "https://",
]

# =========================
# FEATURES
# =========================
STICKER_ENABLED = os.getenv("STICKER_ENABLED", "True").lower() == "true"
PROGRESS_MESSAGE_ENABLED = os.getenv("PROGRESS_MESSAGE_ENABLED", "True").lower() == "true"
FILTER_ENABLED_DEFAULT = os.getenv("FILTER_ENABLED_DEFAULT", "True").lower() == "true"

# =========================
# INDEX SETTINGS
# =========================
INDEX_BATCH_SIZE = int(os.getenv("INDEX_BATCH_SIZE", "100"))
INDEX_CHANNELS = [int(x) for x in os.getenv("INDEX_CHANNELS", "").split(",") if x.strip()]
MAX_CALLBACK_LENGTH = int(os.getenv("MAX_CALLBACK_LENGTH", "50"))
SEASONS_PER_PAGE = int(os.getenv("SEASONS_PER_PAGE", "9"))
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", None)
if FORCE_SUB_CHANNEL:
    FORCE_SUB_CHANNEL = int(FORCE_SUB_CHANNEL)

# =========================
# LOGGING
# =========================
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "bot.log")
MAX_LOG_SIZE = int(os.getenv("MAX_LOG_SIZE", str(10 * 1024 * 1024)))  # 10MB

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

# Prevent duplicate handlers (important in reload environments)
if not root_logger.handlers:
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

logger = logging.getLogger("BOT")
logger.info("Logger initialized")

# =========================
# SILENCE APSCHEDULER LOGS
# =========================
for _name in [
    "apscheduler",
    "apscheduler.scheduler",
    "apscheduler.executors",
    "apscheduler.executors.default",
    "apscheduler.jobstores",
]:
    logging.getLogger(_name).setLevel(logging.CRITICAL)
