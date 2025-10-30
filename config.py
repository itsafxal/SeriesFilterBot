import os
from dotenv import load_dotenv

# Load environment variables from .env file (if it exists)
load_dotenv()

# --- Configuration Class ---
# This class holds all your configuration constants.

class Config:
    # 1. Mandatory/Sensitive Credentials
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    MONGO_URI = os.environ.get("MONGO_URI", "")

    # 2. Lists/Tuples (Parsed from environment variable strings)
    # The default value is None, which will raise an error if not set, which is good for mandatory config.
    try:
        ADMINS = [int(x) for x in os.environ.get("ADMINS", "").split(',')]
        CHANNELS = [int(x) for x in os.environ.get("CHANNELS", "").split(',')]
    except ValueError:
        # Handle case where ADMINS/CHANNELS are not valid integers (e.g., in development)
        ADMINS = []
        CHANNELS = []
        print("WARNING: ADMINS or CHANNELS environment variables are not properly formatted integers.")

    # 3. Simple Variables
    # The default for LOG_CHANNEL is 0 (an invalid ID), which is better than hardcoding.
    LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", 0)) 

