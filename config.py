# config.py

import logging
from pymongo import TEXT 

# --- Bot Configuration ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "8528994143:AAGsOZfOx8P0H9DbnXLcSV8zvR2rRelSds8"
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
