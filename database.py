import pymongo
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ConfigurationError, OperationFailure
import re
import json
import logging
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional

# --- Configuration Imports ---
try:
    from config import DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, LOG_LEVEL 
except ImportError:
    DATABASE_URI = "mongodb://localhost:27017/" 
    DATABASE_NAME = "telegram_indexer"
    COLLECTION_NAME = "series_files"
    LOG_LEVEL = 'INFO'

BROADCAST_USERS_FILE = 'broadcast_users.json' 

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.getLevelName(LOG_LEVEL)
)
logger = logging.getLogger(__name__)

# Global MongoDB Client and Collections
mongo_client: Optional[MongoClient] = None
db = None
series_collection = None
broadcast_users_set = set()

def _load_broadcast_users():
    """Loads broadcast users from the JSON file."""
    global broadcast_users_set
    try:
        with open(BROADCAST_USERS_FILE, 'r') as f:
            data = [int(uid) for uid in json.load(f)]
            broadcast_users_set = set(data)
        logger.info(f"Loaded {len(broadcast_users_set)} broadcast users.")
    except FileNotFoundError:
        logger.warning(f"{BROADCAST_USERS_FILE} not found. Starting with empty set.")
    except json.JSONDecodeError:
        logger.error(f"Error decoding {BROADCAST_USERS_FILE}. Starting with empty set.")
    except Exception as e:
        logger.error(f"Error loading broadcast users: {e}")

def _save_broadcast_users():
    """Saves broadcast users to the JSON file."""
    try:
        with open(BROADCAST_USERS_FILE, 'w') as f:
            json.dump([str(uid) for uid in broadcast_users_set], f)
    except Exception as e:
        logger.error(f"Error saving broadcast users: {e}")


def init_db():
    """Initializes the MongoDB connection and collections."""
    global mongo_client, db, series_collection
    try:
        mongo_client = MongoClient(DATABASE_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command('ismaster')
        logger.info("Successfully connected to MongoDB.")
        
        db = mongo_client[DATABASE_NAME]
        series_collection = db[COLLECTION_NAME]

        # Ensure index for fast searching and a unique index to prevent duplicate file entries
        series_collection.create_index([("series_name", "text")])
        series_collection.create_index([("series_name", pymongo.ASCENDING), ("season", pymongo.ASCENDING), ("episode", pymongo.ASCENDING)], unique=True)


        _load_broadcast_users()
    except (ConnectionFailure, ConfigurationError) as e:
        logger.error(f"MongoDB connection failed: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during DB initialization: {e}")


def save_user_for_broadcast(user_id: int):
    """Adds a user ID to the broadcast set and saves it."""
    global broadcast_users_set
    if user_id not in broadcast_users_set:
        broadcast_users_set.add(user_id)
        _save_broadcast_users()
        logger.info(f"User {user_id} registered for broadcast.")

def get_all_broadcast_users() -> List[int]:
    """Returns a list of all registered user IDs for broadcast."""
    return list(broadcast_users_set)

def add_series_file(series_name: str, season: int, episode: int, channel_id: str, message_id: int) -> bool:
    """
    Saves a series episode file reference to the database.
    Returns True if saved, False if duplicate or failed.
    """
    if series_collection is None:
        return False

    document = {
        "series_name": series_name,
        "season": season,
        "episode": episode,
        "channel_id": channel_id,
        "message_id": message_id,
        "indexed_at": datetime.utcnow()
    }
    
    try:
        series_collection.insert_one(document)
        return True
    except pymongo.errors.DuplicateKeyError:
        return False
    except Exception as e:
        logger.error(f"Error saving series file: {e}")
        return False

def search_series(query: str) -> List[Tuple[str, int, int]]:
    """
    Performs a text search for series files based on the query.
    Returns a list of (series_name, season, episode) tuples.
    """
    if series_collection is None:
        return []

    regex_query = re.escape(query.strip())
    
    results = series_collection.find(
        {"series_name": {"$regex": regex_query, "$options": "i"}}
    ).sort([("series_name", pymongo.ASCENDING), ("season", pymongo.ASCENDING), ("episode", pymongo.ASCENDING)])
    
    data = [(doc['series_name'], doc['season'], doc['episode']) for doc in results]
    return data

def get_unique_seasons_by_name(series_name: str) -> List[int]:
    """
    Gets a sorted list of unique season numbers for a given series name.
    """
    if series_collection is None:
        return []

    seasons = series_collection.distinct("season", {"series_name": series_name})
    return sorted([int(s) for s in seasons])

def get_all_files_by_season(series_name: str, season: int) -> List[Dict[str, Any]]:
    """
    Gets all file documents for a specific series name and season.
    Returns a list of dictionaries.
    """
    if series_collection is None:
        return []

    files = series_collection.find(
        {"series_name": series_name, "season": season}
    ).sort("episode", pymongo.ASCENDING)
    
    return list(files)
