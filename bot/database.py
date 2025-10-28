from pymongo import MongoClient

# NOTE: Replace 'YOUR_MONGO_URI' and 'series_db' as needed
MONGO_URI = "mongodb+srv://UB:UB@cluster0.bkxgfh4.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0" # Example URI
DATABASE_NAME = "series_db"
COLLECTION_NAME = "series_data"

def initialize_db():
    """Connects to MongoDB and returns the series collection."""
    try:
        client = MongoClient(MONGO_URI)
        db = client[DATABASE_NAME]
        print("Successfully connected to MongoDB.")
        return db[COLLECTION_NAME]
    except Exception as e:
        print(f"Error connecting to MongoDB: {e}")
        # Exit or handle the error gracefully
        exit(1)

def add_series_data(collection, series_info):
    """Inserts a new series document into the collection."""
    result = collection.insert_one(series_info)
    return result.inserted_id

def find_series(collection, query):
    """Finds series based on a query (e.g., name, season)."""
    # A robust query would use regex for partial matches:
    # {"title": {"$regex": query, "$options": "i"}}
    return collection.find({"title": query})

# In bot/database.py

# ... (Existing imports and functions: initialize_db, create_indexes, find_series) ...

def add_series_entry(collection, series_info: dict):
    """
    Inserts a new series document (file/episode metadata) into the collection.

    Args:
        collection: The active MongoDB collection object.
        series_info (dict): A dictionary containing the file's metadata.
            Expected keys: 'title', 'season', 'episode', 'file_id', 'link', etc.
            
    Returns:
        str: The MongoDB ObjectId of the newly inserted document.
    """
    try:
        # 1. Insert the document
        result = collection.insert_one(series_info)
        
        # 2. Return the unique ID
        return str(result.inserted_id)
    except Exception as e:
        print(f"Error inserting series entry: {e}")
        return None
