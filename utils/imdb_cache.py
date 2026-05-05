import pickle
import os
import requests

class IMDbCache:
    def __init__(self, cache_file='imdb_cache.pkl'):
        self.cache_file = cache_file
        self.cache = self.load_cache()

    def load_cache(self):
        if os.path.exists(self.cache_file):
            with open(self.cache_file, 'rb') as f:
                return pickle.load(f)
        return {}

    def save_cache(self):
        with open(self.cache_file, 'wb') as f:
            pickle.dump(self.cache, f)

    def fetch_metadata(self, title):
        if title in self.cache:
            return self.cache[title]

        response = requests.get(f'https://www.omdbapi.com/?t={title}&apikey=YOUR_API_KEY')
        if response.status_code == 200:
            metadata = response.json()
            self.cache[title] = metadata
            self.save_cache()
            return metadata
        else:
            return None  # Or handle the error as appropriate

# Example Usage:
# imdb_cache = IMDbCache()
# metadata = imdb_cache.fetch_metadata('Inception')
# print(metadata)