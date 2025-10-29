# config.py

import os

# --- Telegram API Credentials ---
# NOTE: Replace these with your actual values from my.telegram.org
API_ID = 21352077 # Placeholder, replace with your actual API ID
API_HASH = "e4ba74d3f410dffbe848402989e61a04" # Placeholder, replace with your actual API Hash

# --- Bot Credentials ---
# NOTE: Replace this with your actual bot token from BotFather
BOT_TOKEN = "8163888122:AAEuGzXZDxKlcehE2b5Yj4kpayBUo1VaZWs" # Placeholder

# --- Database & Channel Setup ---
MONGO_URI = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0" # Placeholder
DUMP_CHANNEL_ID = int(os.environ.get("DUMP_CHANNEL_ID", -1001996538290))
OWNER_ID = int(os.environ.get("OWNER_ID", 5483128891))

# Session name for the user client used for indexing
INDEXER_SESSION_NAME = "IndexerUser"
