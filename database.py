import pymongo
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ConfigurationError, OperationFailure
import re
import json
import logging
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional

# --- Configuration (MUST BE CONFIGURED IN config.py) ---
# NOTE: These values are placeholders. They must be defined in your config.py
try:
    from config import DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, LOG_LEVEL 
except ImportError:
    # Fallback to local placeholders for standalone testing (replace with your config details)
    DATABASE_URI = "mongodb://localhost:27017/" 
    DATABASE_NAME = "telegram_indexer"
    COLLECTION_NAME = "series_files"
    LOG_LEVEL = 'INFO'

GENERIC_COLLECTION_NAME = "generic_files"
BROADCAST_USERS_FILE = 'broadcast_users.json' 
# --- End Configuration ---

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.getLevelName(LOG_LEVEL)
)
logger = logging.getLogger(__name__)

# Global MongoDB Client and Collections
mongo_client: Optional[MongoClient] = None
db = None
series_collection = None
generic_collection = None
broadcast_users_set = set() # In-memory cache for fast user lookup

def init_db():
    """Initializes the MongoDB connection and collections."""
    global mongo_client, db, series_collection, generic_collection
    try:
        mongo_client = MongoClient(DATABASE_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command('ismaster') # Check connection
        
        db = mongo_client[DATABASE_NAME]
        series_collection = db[COLLECTION_NAME]
        generic_collection = db[GENERIC_COLLECTION_NAME]

        # Ensure indexes for fast querying and uniqueness
        series_collection.create_index(
            [("series_name", pymongo.ASCENDING), ("season", pymongo.ASCENDING), ("episode", pymongo.ASCENDING)], 
            unique=True, 
            name="series_season_episode_unique"
        )
        series_collection.create_index("series_name", name="series_name_idx")
        
        # Index for generic files for efficient duplicate checking
        generic_collection.create_index("file_unique_id", unique=True, name="file_unique_id_idx")
        
        logger.info("MongoDB connection and collections initialized successfully.")
        
        load_broadcast_users() 

    except (ConnectionFailure, ConfigurationError, OperationFailure) as e:
        logger.error(f"Failed to connect or initialize MongoDB: {e}")
        # Re-raise the exception to prevent the bot from starting without a database
        raise

# --- Broadcast User Logic (Using JSON File) ---

def load_broadcast_users():
    """Loads broadcast users from the JSON file into memory."""
    global broadcast_users_set
    try:
        with open(BROADCAST_USERS_FILE, 'r') as f:
            data = json.load(f)
            broadcast_users_set = set(int(uid) for uid in data) 
        logger.info(f"Loaded {len(broadcast_users_set)} broadcast users.")
    except FileNotFoundError:
        logger.warning(f"Broadcast users file '{BROADCAST_USERS_FILE}' not found.")
        broadcast_users_set = set()
    except Exception as e:
        logger.error(f"Error loading broadcast users: {e}")
        broadcast_users_set = set()

def save_broadcast_users():
    """Saves the in-memory set of broadcast users to the JSON file."""
    try:
        with open(BROADCAST_USERS_FILE, 'w') as f:
            json.dump(list(broadcast_users_set), f)
    except Exception as e:
        logger.error(f"Error saving broadcast users: {e}")

def save_user_for_broadcast(user_id: int) -> None:
    """Adds a user ID to the broadcast list if not already present."""
    if user_id not in broadcast_users_set:
        broadcast_users_set.add(user_id)
        save_broadcast_users() 
        logger.info(f"User {user_id} registered for broadcast.")

def get_all_broadcast_users() -> List[int]:
    """Returns all user IDs registered for broadcast."""
    return list(broadcast_users_set)

# --- Series File Logic ---

def add_series_file(series_name: str, season: int, episode: int, channel_id: str, message_id: int) -> bool:
    """
    Saves a series episode file reference to MongoDB.
    Returns True if added, False if duplicate.
    """
    try:
        document = {
            "series_name": series_name,
            "season": season,
            "episode": episode,
            "channel_id": channel_id,
            "message_id": message_id,
            "indexed_at": datetime.utcnow()
        }
        
        series_collection.insert_one(document)
        return True
    except pymongo.errors.DuplicateKeyError:
        return False
    except Exception as e:
        logger.error(f"Error adding series file: {e}")
        return False

def search_series(query: str) -> List[Tuple[str, int, int]]:
    """
    Searches for series based on a regex query.
    Returns a list of (series_name, season, episode).
    """
    regex_pattern = re.compile(f".*{re.escape(query)}.*", re.IGNORECASE)
    
    results = series_collection.find(
        {"series_name": {"$regex": regex_pattern}},
        {"series_name": 1, "season": 1, "episode": 1, "_id": 0}
    ).limit(50) 
    
    return [(d['series_name'], d['season'], d['episode']) for d in results]


def get_unique_seasons_by_name(series_name: str) -> List[int]:
    """
    Gets all unique season numbers for a given series.
    Returns a sorted list of integers.
    """
    pipeline = [
        {"$match": {"series_name": series_name}},
        {"$group": {"_id": "$season"}},
        {"$sort": {"_id": 1}}
    ]
    
    seasons = [doc['_id'] for doc in series_collection.aggregate(pipeline)]
    return seasons


def get_all_files_by_season(series_name: str, season: int) -> List[Dict[str, Any]]:
    """
    Retrieves all file references for a specific season.
    """
    files = series_collection.find(
        {"series_name": series_name, "season": season},
        {"channel_id": 1, "message_id": 1, "_id": 0}
    ).sort("episode", pymongo.ASCENDING)
    
    return list(files)

# --- Generic File Logic ---

def save_generic_file(file_data: Dict[str, Any]) -> Tuple[bool, int]:
    """
    Saves a generic file reference to MongoDB.
    Returns: (is_new_file, status_code)
    status_code: 1=Success, 0=Duplicate, 2=Error
    """
    try:
        generic_collection.insert_one(file_data)
        return True, 1 # Success
    except pymongo.errors.DuplicateKeyError:
        return False, 0 # Duplicate
    except Exception as e:
        logger.error(f"Error saving generic file: {e}")
        return False, 2 # Error
