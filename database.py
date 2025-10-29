# database.py

import pymongo
from pymongo import MongoClient
import re
import json
import configparser

# --- Configuration Loading ---
config = configparser.ConfigParser()
config.read('config.ini')

MONGO_URI = config['DATABASE']['MONGO_URI']
DATABASE_NAME = config['DATABASE']['DATABASE_NAME']
BROADCAST_USERS_FILE = config['DATABASE']['BROADCAST_USERS_FILE']

# Global MongoDB client and collections
client = None
db = None
series_collection = None

def init_db():
    """Initializes the MongoDB connection and collections."""
    global client, db, series_collection
    try:
        client = MongoClient(MONGO_URI)
        client.admin.command('ping') 
        db = client[DATABASE_NAME]
        series_collection = db["series"]
        
        # Ensure indexes for fast searching and uniqueness
        series_collection.create_index([("file_id", 1)], unique=True)
        series_collection.create_index([("series_name", pymongo.TEXT), ("season", 1), ("episode", 1)])
        
        print("MongoDB connection successful.")
        
    except Exception as e:
        print(f"MongoDB connection error. Check MONGO_URI. Error: {e}")

def add_series_file(series_name, season, episode, file_id, channel_id, message_id):
    """Inserts a new series file record using channel_id and message_id for retrieval."""
    if not series_collection: return False
    
    document = {
        "series_name": series_name,
        "season": int(season),
        "episode": int(episode),
        "file_id": file_id,  
        "channel_id": channel_id,
        "message_id": int(message_id),
        "indexed_at": MongoClient.server_info
    }
    
    try:
        series_collection.insert_one(document)
        return True
    except pymongo.errors.DuplicateKeyError:
        return False
    except Exception as e:
        print(f"MongoDB error on adding series: {e}")
        return False

def get_file_details_by_file_id(file_id):
    """Retrieves full document (including channel_id and message_id) using file_id."""
    if not series_collection: return None
    return series_collection.find_one({"file_id": file_id})


def search_series(query):
    """
    Searches series using the 'Pro Search' logic (Name and SXXEYY).
    Returns tuples of (name, season, episode, file_id).
    """
    if not series_collection: return []
    
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
            {'$project': {'_id': 0, 'series_name': 1, 'season': 1, 'episode': 1, 'file_id': 1}},
            {'$sort': {'series_name': 1}}
        ]
    else:
        # Simple name search (fall back)
        pipeline = [
            {'$match': {'series_name': {'$regex': f'.*{re.escape(query)}.*', '$options': 'i'}}},
            {'$project': {'_id': 0, 'series_name': 1, 'season': 1, 'episode': 1, 'file_id': 1}},
            {'$sort': {'series_name': 1, 'season': 1, 'episode': 1}}
        ]
        
    results = list(series_collection.aggregate(pipeline))
    
    return [(res['series_name'], res['season'], res['episode'], res['file_id']) for res in results]


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
    users.add(user_id)
    with open(BROADCAST_USERS_FILE, 'w') as f:
        json.dump(list(users), f)
    
def get_all_broadcast_users():
    """Returns a list of all user IDs for broadcast."""
    return list(load_broadcast_users())

# Initialize the database connection when the module is imported
init_db()
