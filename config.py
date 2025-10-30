import os
import asyncio
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- Configuration Placeholders (Your actual values) ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "8163888122:AAEuGzXZDxKlcehE2b5Yj4kpayBUo1VaZWs"
MONGO_URI = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# List of channel IDs (integers or strings) where files are posted and should be indexed.
CHANNELS = [-1001996538290] 

# --- Bulk Indexing Placeholders ---
# Ensure all ADMINS IDs are valid integers.
ADMINS = [5483128891]  # <-- REPLACE WITH YOUR ADMIN USER ID(s)
LOG_CHANNEL = -1001996538290 # <-- REPLACE WITH YOUR LOG CHANNEL ID

CUSTOM_CAPTION_TEXT = """
✨ **File Sent!**
{file_name}

🔗 **Join our channel for more:**
@{bot_username}
"""

# Concurrency lock for bulk indexing
lock = asyncio.Lock()

# Temporary class for bulk indexing state
class Temp:
    CURRENT = 1
    CANCEL = False

temp = Temp()

# --- Setup Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Media filter for real-time indexing
media_filter = filters.document | filters.video | filters.audio
