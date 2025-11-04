# config.py

import logging
from pymongo import TEXT 

# --- Bot Configuration ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "7661197159:AAEmRKjX-3pDsuG7QZpStO5yYKLy4ip0uVA"
MONGO_URI = "mongodb+srv://ss:ss@seriesfilterbot.p88od4y.mongodb.net/?appName=SeriesFilterBOT"

# --- Indexing Configuration ---
INDEX_CHANNELS = [-1001996538290] 
ADMIN_USERS = [5483128891] # ⚠️ REPLACE ME: Your Telegram User ID(s)

# --- Custom Caption Configuration ---
CUSTOM_CAPTION_TEXT = """
`{file_name}`

🔗 **Please Use Our Bot **
@{bot_username}
"""

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("config")
