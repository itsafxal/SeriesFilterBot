# db_manager.py
from pymongo import MongoClient
from config import MONGO_URI, MONGO_DB_NAME, SERIES_COLLECTION

class DBManager:
    def __init__(self):
        # Initializing MongoClient outside of the async function is fine
        self.client = MongoClient(MONGO_URI)
        self.db = self.client[MONGO_DB_NAME]
        self.collection = self.db[SERIES_COLLECTION]

    # --- Save/Update User Series ---
    def save_user_series(self, user_id, chat_id, series_title, season=None, episode=None):
        """Saves or updates a user's series entry."""
        series_entry = {
            "user_id": user_id,
            "chat_id": chat_id,
            "series_title": series_title,
            "season": season,
            "episode": episode,
            "timestamp": MongoClient.SERVER_INFO 
        }
        # Use series_title and user_id as unique identifier for upsert
        self.collection.update_one(
            {"user_id": user_id, "series_title": series_title},
            {"$set": series_entry},
            upsert=True
        )

    # --- Retrieve All User Series ---
    def get_all_user_series(self, user_id):
        """Retrieves all saved series for a user."""
        return list(self.collection.find({"user_id": user_id}))
