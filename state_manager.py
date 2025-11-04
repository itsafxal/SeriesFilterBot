# state_manager.py
import asyncio
from pymongo import MongoClient
# Assuming config.py contains MONGO_URI and logger
from config import MONGO_URI, logger 

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["SeriesBotDB"]
# Use the collection directly
state_collection = db["bot_state"] 

# Ensure Index (Moved from database.py initialization)
try:
    state_collection.create_index([("_id", 1)]) 
    logger.info("✅ State Collection Index ensured/created successfully.")
except Exception as e:
    logger.error(f"❌ Failed to ensure/create State Index: {e}")

async def save_bot_state(chat_id, last_msg_id):
    """Saves the last processed message ID for a given chat to resume later."""
    def sync_save():
        state_collection.update_one(
            {"_id": chat_id},
            {"$set": {"last_indexed_id": last_msg_id}},
            upsert=True
        )
    await asyncio.to_thread(sync_save)

async def get_bot_state(chat_id):
    """Retrieves the last processed message ID for a given chat."""
    def sync_get():
        doc = state_collection.find_one({"_id": chat_id})
        return doc.get("last_indexed_id") if doc else None
    return await asyncio.to_thread(sync_get)
