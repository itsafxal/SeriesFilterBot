import pymongo
from pymongo import MongoClient
import re
import json
import logging
from datetime import datetime
from config import DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, LOG_LEVEL # COLLECTION_NAME is for series
from telegram import Update, Message

# --- New Configuration for Generic Files ---
GENERIC_COLLECTION_NAME = "generic_files" # Name for the collection storing non-series files
BROADCAST_USERS_FILE = 'broadcast_users.json'

# Global MongoDB client and collections
client = None
db = None
series_collection = None
generic_collection = None # New global for generic files

# Set up logging early
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.getLevelName(LOG_LEVEL)
)

def init_db():
    """Initializes the MongoDB connection and collections."""
    global client, db, series_collection, generic_collection
    try:
        client = MongoClient(DATABASE_URI)
        client.admin.command('ping') # Check connection
        db = client[DATABASE_NAME]
        
        # 1. Series Collection (Existing Logic)
        series_collection = db[COLLECTION_NAME]
        series_collection.create_index([("series_name", pymongo.TEXT), ("season", 1), ("episode", 1)])
        
        # 2. Generic Files Collection (New Logic for Bulk Indexing)
        generic_collection = db[GENERIC_COLLECTION_NAME]
        # Index for search (e.g., on caption) and for duplicate check
        generic_collection.create_index([("file_name", pymongo.TEXT), ("caption", pymongo.TEXT)])
        generic_collection.create_index("file_unique_id", unique=True)
        
        logging.info("MongoDB connection successful. Collections initialized.")
        
    except Exception as e:
        logging.error(f"MongoDB connection failed: {e}")


def add_series_file(series_name, season, episode, channel_id, message_id):
    """Inserts a new series file record after checking for exact episode duplicates."""
    if series_collection is None:
        return False

    # Check for exact episode duplicate
    existing_file = series_collection.find_one({
        "series_name": series_name,
        "season": int(season),
        "episode": int(episode)
    })
    
    if existing_file:
        return False

    document = {
        "series_name": series_name,
        "season": int(season),
        "episode": int(episode),
        "channel_id": channel_id,
        "message_id": int(message_id),
        "indexed_at": datetime.utcnow()
    }
    
    try:
        series_collection.insert_one(document)
        return True
    except Exception as e:
        logging.error(f"MongoDB error on adding series: Invalid document {document} | {e}")
        return False

def save_generic_file(file_data: dict) -> tuple[bool, int]:
    """
    Saves a generic file to the database.
    Returns: (success_bool, status_code)
    status_code: 1=Success, 0=Duplicate, 2=Error
    """
    if generic_collection is None:
        return False, 2
    
    file_unique_id = file_data.get("file_unique_id")
    
    try:
        # Check for duplicate using file_unique_id
        if generic_collection.find_one({"file_unique_id": file_unique_id}):
            return False, 0 # Duplicate
        
        generic_collection.insert_one(file_data)
        return True, 1 # Success
    except pymongo.errors.DuplicateKeyError:
        return False, 0 # Handle race condition or unexpected duplicate
    except Exception as e:
        logging.error(f"MongoDB error on saving generic file: {e}")
        return False, 2 # Error

def find_generic_file(query):
    """Placeholder for future generic file search logic if needed."""
    # For now, we only implement series search as per your original bot structure
    pass

# --- Existing Search and Helper Functions (No change needed) ---

def get_unique_seasons_by_name(series_name):
    """Retrieves a list of unique season numbers for a given series name."""
    if series_collection is None: return []

    pipeline = [
        {'$match': {'series_name': series_name}},
        {'$group': {'_id': '$season'}},
        {'$sort': {'_id': 1}}
    ]
    
    results = series_collection.aggregate(pipeline)
    return [res['_id'] for res in results]

# ... (rest of your existing functions: get_all_files_by_season, search_series, load_broadcast_users, save_user_for_broadcast, get_all_broadcast_users)

def get_all_files_by_season(series_name, season):
    """Retrieves all file details (channel_id, message_id) for a specific season."""
    if series_collection is None: return []

    files_cursor = series_collection.find(
        {'series_name': series_name, 'season': int(season)},
        {'channel_id': 1, 'message_id': 1, '_id': 0}
    ).sort('episode', 1)

    return list(files_cursor)

def search_series(query):
    """
    Searches series using the 'Pro Search' logic (Name and SXXEYY) or simple name search.
    Returns tuples of (name, season, episode).
    """
    if series_collection is None: return []
    
    # Pro Search Parsing (Pattern: NAME SXXEYY)
    match = re.search(r'(.*?)\s*[sS](\d+)[eE](\d+)', query.strip())
    
    if match:
        name_part = match.group(1).strip()
        season = int(match.group(2))
        episode = int(match.group(3))
        
        pipeline = [
            {'$match': {
                'series_name': {'$regex': f'.*{re.escape(name_part)}.*', '$options': 'i'},
                'season': season,
                'episode': episode
            }},
            {'$project': {'_id': 0, 'series_name': 1, 'season': 1, 'episode': 1}},
            {'$sort': {'series_name': 1}}
        ]
    else:
        # Simple name search (fall back)
        pipeline = [
            {'$match': {'series_name': {'$regex': f'.*{re.escape(query)}.*', '$options': 'i'}}},
            {'$project': {'_id': 0, 'series_name': 1, 'season': 1, 'episode': 1}},
            {'$sort': {'series_name': 1, 'season': 1, 'episode': 1}}
        ]
        
    results = list(series_collection.aggregate(pipeline))
    
    return [(res['series_name'], res['season'], res['episode']) for res in results]


# --- User Data for Broadcast (JSON/File Storage) ---
def load_broadcast_users():
    """Loads user IDs saved for future broadcasts."""
    try:
        with open(BROADCAST_USERS_FILE, 'r') as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_user_for_broadcast(user_id):
    """Adds a user ID to the broadcast list."""
    users = load_broadcast_users()
    user_id_str = str(user_id)
    if user_id_str not in users:
        users.add(user_id_str)
        with open(BROADCAST_USERS_FILE, 'w') as f:
            json.dump(list(users), f)
        return True
    return False
    
def get_all_broadcast_users():
    """Returns a list of all user IDs for broadcast (converted back to int for sending)."""
    return [int(uid) for uid in load_broadcast_users()]
