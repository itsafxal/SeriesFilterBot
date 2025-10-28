from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure
import os

# --- Configuration ---
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DATABASE_NAME = "SeriesVaultBotDB"
STATE_COLLECTION = "user_states"
CONTENT_COLLECTION = "series_content"
ADMIN_USER_ID = 123456789 # <--- CHANGE THIS TO YOUR TELEGRAM ADMIN ID

class Database:
    """Handles MongoDB connection, indexing, and content storage."""
    def __init__(self):
        self.client = None
        self.db = None
        self.states = None
        self.content = None
        self._connect()
        self._ensure_indexes()

    def _connect(self):
        """Initializes the pymongo connection."""
        try:
            self.client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping') 
            self.db = self.client[DATABASE_NAME]
            self.states = self.db[STATE_COLLECTION]
            self.content = self.db[CONTENT_COLLECTION]
            print("MongoDB connection successful.")
        except ConnectionFailure as e:
            print(f"MongoDB Connection Failed: {e}")
        except Exception as e:
            print(f"MongoDB Error: {e}")
            
    def _ensure_indexes(self):
        """Creates smooth and fast indexes for content querying."""
        if not self.content: return
        try:
            # Index 1: For quick lookup by series name, season, and episode (unique content)
            self.content.create_index(
                [("series_name", 1), ("season", 1), ("episode", 1)], 
                unique=True, 
                name="series_episode_index"
            )
            # Index 2: For faster filtering on series name alone (e.g., in a list command)
            self.content.create_index("series_name", name="series_name_index")
            print("MongoDB indexes ensured for faster access.")
        except OperationFailure as e:
            print(f"Index creation failed: {e}")

    # --- Series Content Operations (Series-Only) ---

    def save_series_file(self, series_data: dict):
        """Saves a series video file document."""
        if not self.content: return False
        
        # Ensure all required fields for a series are present
        required_keys = ["series_name", "season", "episode", "file_id"]
        if not all(k in series_data for k in required_keys):
            print("Missing required series metadata.")
            return False

        try:
            # Use series_name, season, and episode as a unique identifier for upsert
            query = {
                "series_name": series_data["series_name"],
                "season": series_data["season"],
                "episode": series_data["episode"]
            }
            update = {"$set": series_data}
            
            result = self.content.update_one(query, update, upsert=True)
            return result.upserted_id is not None or result.modified_count > 0
        except OperationFailure as e:
            print(f"Failed to save series file: {e}")
            return False

    # --- User State Operations (For ConversationHandler persistence) ---

    def save_state(self, chat_id: int, user_id: int, state: str, data: dict):
        """Saves or updates the user's conversation state."""
        if not self.states: return
        key = {"chat_id": chat_id, "user_id": user_id}
        update_data = {"$set": {"state": state, "data": data}}
        self.states.update_one(key, update_data, upsert=True)

    def get_state(self, chat_id: int, user_id: int) -> dict:
        """Retrieves the conversation state."""
        if not self.states: return {"state": -1, "data": {}}
        key = {"chat_id": chat_id, "user_id": user_id}
        document = self.states.find_one(key)
        return {"state": document.get("state") if document else -1, "data": document.get("data", {})}

    def clear_state(self, chat_id: int, user_id: int):
        """Clears the conversation state."""
        if not self.states: return
        self.states.delete_one({"chat_id": chat_id, "user_id": user_id})

    # --- Stats Operations (For Admin Command) ---

    def get_stats(self) -> dict:
        """Returns bot statistics."""
        if not self.content: 
            return {"total_series_episodes": 0, "total_series": 0, "db_status": "Disconnected"}
            
        total_episodes = self.content.count_documents({})
        # Use aggregation to count distinct series names
        distinct_series = self.content.aggregate([
            {"$group": {"_id": "$series_name"}},
            {"$count": "total_series"}
        ])
        
        total_series = next(distinct_series, {}).get("total_series", 0)
        
        # NOTE: Tracking 'remaining database space' requires admin access to the MongoDB server,
        # which is usually not possible from a bot application. We track document counts instead.

        return {
            "total_series_episodes": total_episodes,
            "total_series": total_series,
            "db_status": "Connected"
        }

    def close(self):
        """Closes the MongoDB connection."""
        if self.client:
            self.client.close()
            print("MongoDB connection closed.")

db = Database()
