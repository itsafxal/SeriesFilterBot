# config.py

# --- Pyrogram Credentials ---
API_ID = 1234567 # Replace with your actual API ID
API_HASH = "YOUR_API_HASH" # Replace with your actual API Hash
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN" # Replace with your bot token

# --- MongoDB Credentials ---
MONGO_URI = "YOUR_MONGODB_CONNECTION_URI"

# Database and Collection names
MONGO_DB_NAME = "series_data"
SERIES_COLLECTION = "user_series" 

# Conversation states (for Pyrogram's Filters/Data storage)
# Pyrogram typically manages state via conversation, but here we'll use a simple flag for context
