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

# --- EXAMPLE USAGE (Not part of the bot logic, just for testing) ---
# if __name__ == '__main__':
#     series_collection = initialize_db()
#     new_series = {
#         "title": "The Mandalorian",
#         "season": 3,
#         "episode": 8,
#         "link": "some_telegram_file_link",
#         "tags": ["sci-fi", "star-wars"]
#     }
#     add_series_data(series_collection, new_series)
#     print(f"Added series with ID: {new_series['_id']}")
