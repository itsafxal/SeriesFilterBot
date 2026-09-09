# config.py — validated environment, global bot identity cache
import os
import logging
from logging.handlers import RotatingFileHandler
from itertools import cycle
from dotenv import load_dotenv

load_dotenv()

def _get_int_env(name: str, default: int = 0) -> int:
    val = os.getenv(name, str(default))
    try:
        return int(val)
    except ValueError:
        raise SystemExit(f"❌ {name} must be an integer, got '{val}'")

# Bot credentials
API_ID = _get_int_env("API_ID", 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

if not all((API_ID, API_HASH, BOT_TOKEN)):
    raise SystemExit("❌ Missing API_ID, API_HASH, or BOT_TOKEN")

# MongoDB Settings & Aliases
MONGO_URI_CONFIG = (
    os.getenv("MONGO_URI_CONFIG", "")
    or os.getenv("DATABASE_URI", "")
    or os.getenv("MONGO_DB_URI", "")
    or os.getenv("MONGO_URI", "")
    or os.getenv("MONGO_URL", "")
    or os.getenv("DATABASE_URL", "")
    or os.getenv("DB_URI", "")
    or os.getenv("DB_URL", "")
)
MONGO_URI_DB1    = os.getenv("MONGO_URI_DB1", "") or MONGO_URI_CONFIG
MONGO_URI_DB2    = os.getenv("MONGO_URI_DB2", "") or MONGO_URI_CONFIG
DATABASE_URI     = MONGO_URI_CONFIG
MONGO_DB_URI     = MONGO_URI_CONFIG
DATABASE_URL     = MONGO_URI_CONFIG
MONGO_URI        = MONGO_URI_CONFIG
MONGO_URL        = MONGO_URI_CONFIG
DB_URI           = MONGO_URI_CONFIG
DB_URL           = MONGO_URI_CONFIG
DATABASE_NAME    = os.getenv("DATABASE_NAME", "SeriesBotConfig")

# Export all aliases to os.environ so third-party/legacy modules find them
for _alias in ("DATABASE_URI", "MONGO_DB_URI", "DATABASE_URL", "MONGO_URI", "MONGO_URL", "DB_URI", "DB_URL", "MONGO_URI_CONFIG"):
    if MONGO_URI_CONFIG and not os.getenv(_alias):
        os.environ[_alias] = MONGO_URI_CONFIG

if not MONGO_URI_CONFIG:
    raise SystemExit("❌ Missing MONGO_URI_CONFIG or DATABASE_URI in environment or .env file.")

# Admin IDs
ADMIN_USERS = [
    int(uid.strip())
    for uid in os.getenv("ADMIN_USERS", "").replace(" ", ",").split(",")
    if uid.strip().isdigit()
]

# Images
START_IMG      = os.getenv("START_IMG", "https://files.catbox.moe/ivj3km.jpg")
SPELL_IMG      = os.getenv("SPELL_IMG", "https://files.catbox.moe/krlzjs.jpg")
DEFAULT_POSTER = os.getenv("DEFAULT_POSTER", "https://files.catbox.moe/rjj4do.jpg")

# API keys (rotating)
import re
TMDB_API_KEYS = [k.strip() for k in re.split(r'[,\s]+', os.getenv("TMDB_API_KEYS", "")) if k.strip()]
OMDB_API_KEYS = [k.strip() for k in re.split(r'[,\s]+', os.getenv("OMDB_API_KEYS", "")) if k.strip()]
if not TMDB_API_KEYS:
    TMDB_API_KEYS = [""]
if not OMDB_API_KEYS:
    OMDB_API_KEYS = [""]

_tmdb_cycle = cycle(TMDB_API_KEYS)
_omdb_cycle = cycle(OMDB_API_KEYS)

def TMDB_API_KEY() -> str:
    return next(_tmdb_cycle)

def OMDB_API_KEY() -> str:
    return next(_omdb_cycle)

# TMDB config
TMDB_CACHE_DB        = "/tmp/TMDB_cache.db"
TMDB_IMAGE_BASE      = "https://image.tmdb.org/t/p/w500"
TMDB_IMAGE_BASE_WIDE = "https://image.tmdb.org/t/p/w780"
POSTER_ENABLED   = os.getenv("POSTER_ENABLED", "True").lower() == "true"
POSTER_LANDSCAPE = os.getenv("POSTER_LANDSCAPE", "False").lower() == "true"

# Caption cleanup
DEFAULT_CAPTION_CLEAN_KEYWORDS = [
    "@", "t.me", "Telegram", "www.", "http://", "https://",
]

# Features
STICKER_ENABLED           = os.getenv("STICKER_ENABLED", "True").lower() == "true"
PROGRESS_MESSAGE_ENABLED  = os.getenv("PROGRESS_MESSAGE_ENABLED", "True").lower() == "true"
FILTER_ENABLED_DEFAULT    = os.getenv("FILTER_ENABLED_DEFAULT", "True").lower() == "true"

# Index settings
INDEX_CHANNELS      = [
    int(x) for x in os.getenv("INDEX_CHANNELS", "").split(",")
    if x.strip().lstrip('-').isdigit()
]
SEASONS_PER_PAGE    = _get_int_env("SEASONS_PER_PAGE", 9)

# Logging
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "bot.log")
MAX_LOG_SIZE  = _get_int_env("MAX_LOG_SIZE", 10 * 1024 * 1024)

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
file_handler = RotatingFileHandler(
    LOG_FILE_PATH, maxBytes=MAX_LOG_SIZE, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if not root_logger.handlers:
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

logger = logging.getLogger("BOT")
logger.info("Logger initialized")

# Silence noisy loggers
for noisy in ("apscheduler", "apscheduler.scheduler", "apscheduler.executors",
              "apscheduler.executors.default", "apscheduler.jobstores"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)

# Global bot identity (set after start by main.py)
bot_id: int | None = None
bot_username: str | None = None
bot_first_name: str | None = None
