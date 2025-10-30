import os
from dotenv import load_dotenv

# Load environment variables from .env file (if it exists)
load_dotenv()

# --- Configuration Class ---
# This class holds all your configuration constants.

class Config:
    # 1. Mandatory/Sensitive Credentials
    API_ID = int(os.environ.get("API_ID", 28643132))
    API_HASH = os.environ.get("API_HASH", "510c3009ea589830a89e045bf8aae656")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "8163888122:AAEuGzXZDxKlcehE2b5Yj4kpayBUo1VaZWs")
    MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")

    # 2. Lists/Tuples (Parsed from environment variable strings)
    # The default value is None, which will raise an error if not set, which is good for mandatory config.
    try:
        ADMINS = [int(x) for x in os.environ.get("ADMINS", "5483128891").split(',')]
        CHANNELS = [int(x) for x in os.environ.get("CHANNELS", "-1001996538290").split(',')]
    except ValueError:
        # Handle case where ADMINS/CHANNELS are not valid integers (e.g., in development)
        ADMINS = []
        CHANNELS = []
        print("WARNING: ADMINS or CHANNELS environment variables are not properly formatted integers.")

    # 3. Simple Variables
    # The default for LOG_CHANNEL is 0 (an invalid ID), which is better than hardcoding.
    LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", -1001996538290)) 
