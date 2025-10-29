import pymongo
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ConfigurationError, OperationFailure # Use ConnectionFailure
# ----------------
import re
import json
import logging
from datetime import datetime
from config import DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, LOG_LEVEL # COLLECTION_NAME is for series

# --- New Configuration for Generic Files ---
GENERIC_COLLECTION_NAME = "generic_files" # Name for the collection storing non-series files
BROADCAST_USERS_FILE = 'broadcast_users.json' # NOTE: In a production MongoDB environment, broadcast users should also be stored in Mongo for better scalability and consistency. Keeping the original file-based logic for users for simplicity based on the structure provided.

# Global MongoDB client and collections
client = None
db = None
series_collection = None
generic_collection = None # New global for generic files
broadcast_collection = None # New global for broadcast users

# Set up logging early
logger = logging.getLogger(__name__)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.getLevelName(LOG_LEVEL)
)

def init_db():
    """Initializes the MongoDB connection and collections."""
    global client, db, series_collection, generic_collection, broadcast_collection
    try:
        # NOTE: Added serverSelectionTimeoutMS for robustness
        client = MongoClient(DATABASE_URI, serverSelectionTimeoutMS=5000)
        client.admin.command('ping') # Check connection
        
        db = client[DATABASE_NAME]
        
        # 1. Series Collection Setup
        series_collection = db[COLLECTION_NAME]
        series_collection.create_index([
            ('series_name', pymongo.TEXT),
            ('season', 1), 
            ('episode', 1)
        ], name='series_search_index')
        # Ensure files are unique to prevent re-indexing duplicates
        series_collection.create_index([
            ('channel_id', 1),
            ('message_id', 1)
        ], unique=True, name='unique_message_id_index')
        
        # 2. Generic Files Collection Setup
        generic_collection = db[GENERIC_COLLECTION_NAME]
        # Unique index on file_unique_id (Telegram's unique identifier)
        generic_collection.create_index('file_unique_id', unique=True, name='unique_file_id')
        
        # 3. Broadcast Users Collection Setup
        broadcast_collection = db['broadcast_users']
        broadcast_collection.create_index('user_id', unique=True, name='unique_user_id')
        
        logger.info("MongoDB connection and collections successfully initialized.")

    except ConnectionError as e:
        logger.error(f"MongoDB connection failed: {e}")
        raise
    except ConfigurationError as e:
        logger.error(f"MongoDB configuration error: {e}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred during DB initialization: {e}")
        raise

# --- Broadcast Users Functions ---

def save_user_for_broadcast(user_id: int) -> bool:
    """Saves a user ID for broadcast messages (or updates last seen)."""
    if broadcast_collection is None: return False
    try:
        result = broadcast_collection.update_one(
            {'user_id': user_id},
            {'$set': {'last_active': datetime.utcnow()}},
            upsert=True
        )
        # Check if a new document was inserted (a new user)
        if result.upserted_id:
             logger.info(f"New user registered for broadcast: {user_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to save user {user_id} for broadcast: {e}")
        return False

def get_all_broadcast_users() -> list[int]:
    """Retrieves all registered user IDs for broadcasting."""
    if broadcast_collection is None: return []
    try:
        # Fetch only the user_id field
        return [doc['user_id'] for doc in broadcast_collection.find({}, {'user_id': 1})]
    except Exception as e:
        logger.error(f"Failed to retrieve broadcast users: {e}")
        return []

# --- Series Files Functions ---

def add_series_file(series_name: str, season: int, episode: int, channel_id: str, message_id: int) -> bool:
    """Adds a series file entry to the database, checking for duplicates."""
    if series_collection is None: return False
    try:
        file_doc = {
            "series_name": series_name,
            "season": season,
            "episode": episode,
            "channel_id": channel_id,
            "message_id": message_id,
            "indexed_at": datetime.utcnow()
        }
        # Use insert_one and catch DuplicateKeyError to handle duplicates
        series_collection.insert_one(file_doc)
        return True # File added successfully
    except pymongo.errors.DuplicateKeyError:
        # File (message_id/channel_id pair) already exists
        return False
    except Exception as e:
        logger.error(f"Failed to add series file: {e}")
        return False

def search_series(query: str) -> list[tuple[str, int, int]]:
    """Searches for series files matching the query using MongoDB's text index."""
    if series_collection is None: return []
    try:
        # Escape special regex characters in the query for better control, then use regex search
        escaped_query = re.escape(query.strip())
        regex = re.compile(f".*{escaped_query}.*", re.IGNORECASE)
        
        # Use a case-insensitive regex search on the series_name
        results = series_collection.find(
            {'series_name': {'$regex': regex}},
            {'series_name': 1, 'season': 1, 'episode': 1}
        )
        # The result in the main_bot.py suggests returning a list of tuples: (series_name, season, episode, ...)
        return [(doc['series_name'], doc['season'], doc['episode']) for doc in results]
    except Exception as e:
        logger.error(f"Series search failed: {e}")
        return []

def get_unique_seasons_by_name(series_name: str) -> list[int]:
    """Gets a sorted list of unique season numbers for a given series."""
    if series_collection is None: return []
    try:
        # Use the distinct command to get all unique season values
        seasons = series_collection.distinct('season', {'series_name': series_name})
        # Remove any non-integer or zero season values that might have crept in
        return sorted([s for s in seasons if isinstance(s, int) and s > 0])
    except Exception as e:
        logger.error(f"Failed to get unique seasons for {series_name}: {e}")
        return []

def get_all_files_by_season(series_name: str, season: int) -> list[dict]:
    """Retrieves all file entries (channel_id, message_id) for a specific season."""
    if series_collection is None: return []
    try:
        # Fetch only the required fields, sorted by episode number
        files = series_collection.find(
            {'series_name': series_name, 'season': season},
            {'channel_id': 1, 'message_id': 1, '_id': 0}
        ).sort('episode', pymongo.ASCENDING)
        
        return list(files)
    except Exception as e:
        logger.error(f"Failed to get files for {series_name} S{season}: {e}")
        return []

# --- Generic Files Functions ---

def save_generic_file(file_data: dict) -> tuple[bool, int]:
    """
    Saves a generic file (non-series format) to the database.
    Returns: (success_status: bool, error_code: int)
    error_code: 1=Success, 0=Duplicate, 2=Other Error
    """
    if generic_collection is None: return False, 2
    try:
        # Insert with checks for duplicate file_unique_id
        generic_collection.insert_one(file_data)
        return True, 1
    except pymongo.errors.DuplicateKeyError:
        # File (file_unique_id) already exists
        return False, 0
    except Exception as e:
        logger.error(f"Failed to save generic file {file_data.get('file_name', 'NoName')}: {e}")
        return False, 2
