# database/mongo_db.py

import asyncio
import logging
from pymongo import MongoClient, TEXT, DESCENDING
from config import MONGO_URI # Assume MONGO_URI is imported from config

logger = logging.getLogger(__name__)

# --- Database Setup (Synchronous PyMongo) ---
mongo_client = None
db = None
files_collection = None
state_collection = None

def setup_mongo_db():
    """Initializes MongoDB client, collections, and ensures indexes."""
    global mongo_client, db, files_collection, state_collection
    
    if mongo_client:
        # Already initialized
        return

    try:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client["SeriesBotDB"]
        files_collection = db["files"]
        state_collection = db["bot_state"]

        # Ensure indices are created
        # 1. Compound index for fast unique checks and sorting
        files_collection.create_index([("normalized_name", 1), ("season", 1)], background=True)
        
        # 2. Ensure the required TEXT INDEX for fuzzy search is created
        files_collection.create_index(
            [("normalized_name", TEXT)], 
            name="series_name_text_index", 
            default_language='english',
            background=True, 
            unique=False
        )
        
        logger.info("✅ MongoDB Indexes ensured/created successfully.")
        return True
    except Exception as e:
        logger.error(f"❌ Error setting up MongoDB. The bot may fail: {e}")
        return False

###################################################
## DATABASE WRAPPER FUNCTIONS (PyMongo via asyncio.to_thread) ##
###################################################

async def save_bot_state(chat_id, last_msg_id):
    """Saves the last processed message ID for a given chat to resume later."""
    def sync_save():
        # Requires state_collection to be set up
        state_collection.update_one(
            {"_id": chat_id},
            {"$set": {"last_indexed_id": last_msg_id}},
            upsert=True
        )
        logger.info(f"DB: Updated state for {chat_id} to ID {last_msg_id}")
    if not state_collection: setup_mongo_db()
    await asyncio.to_thread(sync_save)

async def get_bot_state(chat_id):
    """Retrieves the last processed message ID for a given chat."""
    def sync_get():
        # Returns the stored ID, or None if the chat hasn't been indexed before
        doc = state_collection.find_one({"_id": chat_id})
        return doc.get("last_indexed_id") if doc else None
    
    if not state_collection: setup_mongo_db()
    return await asyncio.to_thread(sync_get)


async def search_series(query: str, re_search_module):
    """
    Searches MongoDB for unique series names matching the query. 
    Prioritizes text index for better fuzzy matching.
    Takes 're_search_module' (the 're' import) to keep this file self-contained.
    """
    def sync_search():
        # 1. Text Search 
        pipeline_text_search = [
            {"$match": {"$text": {"$search": query}}},
            {"$project": {
                "normalized_name": 1,
                "search_score": {"$meta": "textScore"} 
            }},
            {"$sort": {"search_score": DESCENDING}}, 
            {"$group": {"_id": "$normalized_name"}},
            {"$project": {"_id": 0, "name": "$_id"}}
        ]
        
        try:
            results = list(files_collection.aggregate(pipeline_text_search))
            if results:
                return results
        except Exception as e:
            logger.warning(f"Text search failed (Index issue likely): {e}. Falling back to Regex.")
        
        # 2. Fallback to the original regex search
        regex_query = re_search_module.escape(query)
        pipeline_regex_search = [
            {"$match": {"normalized_name": {"$regex": regex_query, "$options": "i"}}},
            {"$group": {"_id": "$normalized_name"}},
            {"$project": {"_id": 0, "name": "$_id"}}
        ]
        return list(files_collection.aggregate(pipeline_regex_search))
        
    if not files_collection: setup_mongo_db()
    results = await asyncio.to_thread(sync_search)
    return [doc["name"] for doc in results]

async def get_unique_seasons_by_name(series_name: str):
    """Gets unique season numbers for a given series."""
    def sync_get_seasons():
        seasons = files_collection.distinct("season", {"normalized_name": series_name})
        seasons = sorted([s for s in seasons if s > 0])
        return seasons
        
    if not files_collection: setup_mongo_db()
    return await asyncio.to_thread(sync_get_seasons)

async def get_all_files_by_season(series_name: str, season: int):
    """Gets file details (channel_id, message_id, file_name) for a specific season."""
    def sync_get_files():
        files = files_collection.find(
            {"normalized_name": series_name, "season": season},
            {"channel_id": 1, "message_id": 1, "file_name": 1, "_id": 0} 
        ).sort("episode", 1)
        return list(files)
        
    if not files_collection: setup_mongo_db()
    return await asyncio.to_thread(sync_get_files)
    
async def save_file_to_db(file_doc: dict):
    """Inserts/updates a single file document into MongoDB. Returns True if saved/modified."""
    def sync_insert_one():
        try:
            result = files_collection.update_one(
                {"channel_id": file_doc["channel_id"], "message_id": file_doc["message_id"]},
                {"$set": file_doc},
                upsert=True
            )
            
            if result.upserted_id or result.modified_count > 0:
                logger.info(f"DB: INSERTED/UPDATED file. Msg ID: {file_doc['message_id']}")
                return True
            else:
                logger.debug(f"DB: SKIPPED (Duplicate unchanged). Msg ID: {file_doc['message_id']}")
                return False # False means duplicate/no change
        except Exception as e:
            logger.error(f"Failed to insert/update file doc: {e}")
            return False # False means error
            
    if not files_collection: setup_mongo_db()
    return await asyncio.to_thread(sync_insert_one)
