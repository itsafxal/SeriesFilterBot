"""
config.py — OPTIMIZED EDITION
================================
All bot configuration in one place.
"""

import logging
from logging.handlers import RotatingFileHandler

# ── BOT CREDENTIALS ────────────────────────────────────────────
API_ID    = 28643132
API_HASH  = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "8333630020:AAE4ZBaAjzU_Qs5CyxHpkLs6vwn1jJxoXco"

# ── MONGO DB (3 separate URIs for 3 logical DBs) ───────────────
MONGO_URI_CONFIG = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?appName=Cluster0"
MONGO_URI_DB1    = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?appName=Cluster0"
MONGO_URI_DB2    = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?appName=Cluster0"

# ── ADMIN ──────────────────────────────────────────────────────
ADMIN_USERS = [5794562450]

# ── IMAGES ────────────────────────────────────────────────────
START_IMG      = "https://files.catbox.moe/ivj3km.jpg"
SPELL_IMG      = "https://files.catbox.moe/krlzjs.jpg"
DEFAULT_POSTER = "https://files.catbox.moe/rjj4do.jpg"

# ── POSTER / TMDB / OMDB ──────────────────────────────────────
TMDB_API_KEY         = "0e027f75bc7f6a14139ad3e6fe76ed97"
OMDB_API_KEY         = "f93ab313"
TMDB_CACHE_DB        = "/tmp/TMDB_cache.db"
TMDB_IMAGE_BASE      = "https://image.tmdb.org/t/p/w500"   # portrait
TMDB_IMAGE_BASE_WIDE = "https://image.tmdb.org/t/p/w780"   # landscape

# ── POSTER DEFAULTS (admin can override via /adminsetting) ────
POSTER_ENABLED   = True    # show/hide poster
POSTER_LANDSCAPE = False   # False=portrait, True=landscape

# ── FILE CAPTION FORMAT ───────────────────────────────────────
# Available placeholders: {series}, {file_name}, {season},
#   {episode}, {language}, {quality}, {year}, {genre}, {rating}
DEFAULT_FILE_CAPTION = (
    "<b>{series}</b>\n\n"
    "<b>{file_name}</b>\n\n"
    "📅 Season <b>{season}</b>  •  Episode <b>{episode}</b>\n"
    "🌐 {language}  •  🎞 {quality}"
)

# ── CAPTION CLEANING ──────────────────────────────────────────
# Keywords stripped from file_name_clean / caption_clean at save time.
# Admin can add/remove via admin settings.
DEFAULT_CAPTION_CLEAN_KEYWORDS = [
    "@", "t.me", "Telegram", "www.", "http://", "https://",
]

# ── STICKER & PROGRESS ────────────────────────────────────────
STICKER_ENABLED          = True   # season start/end stickers
PROGRESS_MESSAGE_ENABLED = True   # "Preparing N files…" — deleted when done

# ── FILTER DEFAULT ────────────────────────────────────────────
FILTER_ENABLED_DEFAULT = True

# ── INDEX ─────────────────────────────────────────────────────
INDEX_BATCH_SIZE    = 100          # files per dedup/save batch
INDEX_CHANNELS      = []

# ── UI LIMITS ─────────────────────────────────────────────────
MAX_CALLBACK_LENGTH = 50
SEASONS_PER_PAGE    = 9

# ── FSub ──────────────────────────────────────────────────────
FORCE_SUB_CHANNEL = None

# ── LOGGING ───────────────────────────────────────────────────
LOG_FILE_PATH = "bot.log"
MAX_LOG_SIZE  = 10 * 1024 * 1024   # 10 MB

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

# ── Silence noisy third-party loggers ────────────────────────
for _name in [
    "apscheduler",
    "apscheduler.scheduler",
    "apscheduler.executors",
    "apscheduler.executors.default",
    "apscheduler.jobstores",
]:
    logging.getLogger(_name).setLevel(logging.CRITICAL)
