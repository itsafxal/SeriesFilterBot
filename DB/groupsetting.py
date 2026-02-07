from pymongo import MongoClient
from config import MONGO_URI

mongo = MongoClient(MONGO_URI)
db = mongo["SeriesBotDB"]
groups = db["groups"]

DEFAULT = {
    "connected": True,
    "series_search": True,
    "imdb": True,
    "poster": True,
    "strict": True,
    "force_sub": False,
    "max_results": 10,
    "delete_suggestions": True,
    "suggestion_timer": 300,
    "result_timer": 300
}


async def ensure_group(chat_id: int):
    if not groups.find_one({"group_id": chat_id}):
        groups.insert_one({
            "group_id": chat_id,
            **DEFAULT
        })


async def get_group(chat_id: int):
    await ensure_group(chat_id)
    return groups.find_one({"group_id": chat_id})


async def set_group(chat_id: int, key: str, value):
    groups.update_one(
        {"group_id": chat_id},
        {"$set": {key: value}},
        upsert=True
    )


async def reset_group(chat_id: int):
    groups.update_one(
        {"group_id": chat_id},
        {"$set": DEFAULT},
        upsert=True
    )


async def authorise(chat_id: int):
    await set_group(chat_id, "connected", True)


async def unauthorise(chat_id: int):
    await set_group(chat_id, "connected", False)
