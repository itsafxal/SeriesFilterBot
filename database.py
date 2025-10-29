import pymongo
from pymongo import MongoClient
import re
import json
import logging
from datetime import datetime
# Ensure you import config variables
from config import DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, LOG_LEVEL # Added INDEX_CHANNEL_ID

# NOTE: Since BROADCAST_USERS_FILE is not in config.py, we define it here as a local file path.
BROADCAST_USERS_FILE = 'broadcast_users.json'

# Global MongoDB client and collections
client = None
db = None
series_collection = None

# Set up logging early 
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.getLevelName(LOG_LEVEL)
)

def init_db():
    """Initializes the MongoDB connection and collections."""
    global client, db, series_collection
    try:
        client = MongoClient(DATABASE_URI)
        client.admin.command('ping') # Check connection
        db = client[DATABASE_NAME]
        series_collection = db[COLLECTION_NAME] 
        
        # Ensure index for fast searching and uniqueness on episode info
        series_collection.create_index([("series_name", pymongo.TEXT), ("season", 1), ("episode", 1)])
        
        logging.info("MongoDB connection successful.")
        
    except Exception as e:
        logging.error(f"MongoDB connection failed: {e}")

# --- Duplicate Fix Implemented Here ---
def add_series_file(series_name, season, episode, channel_id, message_id):
    """Inserts a new series file record after checking for exact episode duplicates."""
    if series_collection is None: 
        return False 
    
    # Check if this exact episode already exists by (name, season, episode).
    existing_file = series_collection.find_one({
        "series_name": series_name,
        "season": int(season),
        "episode": int(episode)
    })
    
    if existing_file:
        # File is skipped because an entry for this exact episode already exists.
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

# --- New Functions for Autofilter Menu ---

def get_unique_seasons_by_name(series_name):
    """Retrieves a list of unique season numbers for a given series name."""
    if series_collection is None: return []

    # Use aggregation to find distinct season numbers, sorted numerically
    pipeline = [
        {'$match': {'series_name': series_name}},
        {'$group': {'_id': '$season'}},
        {'$sort': {'_id': 1}}
    ]
    
    results = series_collection.aggregate(pipeline)
    return [res['_id'] for res in results]


def get_all_files_by_season(series_name, season):
    """Retrieves all file details (channel_id, message_id) for a specific season."""
    if series_collection is None: return []

    files_cursor = series_collection.find(
        {'series_name': series_name, 'season': int(season)},
        {'channel_id': 1, 'message_id': 1, '_id': 0}
    ).sort('episode', 1)

    return list(files_cursor)

def get_file_details_by_file_id(file_id):
    """(Kept for compatibility, but the document no longer stores file_id if indexed via channel_post_handler)"""
    if series_collection is None: return None
    # NOTE: This function's usage is discouraged as we remove the storage of file_id
    # We will search by file_id if it's there from old data.
    return series_collection.find_one({"file_id": file_id})

def search_series(query):
    """
    Searches series using the 'Pro Search' logic (Name and SXXEYY).
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
                # Use regex for partial matching on the name part
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
    # User ID must be converted to string for JSON if not already
    user_id_str = str(user_id)
    if user_id_str not in users:
        users.add(user_id_str)
        with open(BROADCAST_USERS_FILE, 'w') as f:
            json.dump(list(users), f)
        return True
    return False # User already exists
    
def get_all_broadcast_users():
    """Returns a list of all user IDs for broadcast (converted back to int for sending)."""
    return [int(uid) for uid in load_broadcast_users()]
    
