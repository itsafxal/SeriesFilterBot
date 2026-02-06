import asyncio
from pymongo import MongoClient
from config import MONGO_URI

mongo = MongoClient(MONGO_URI)
db = mongo["SeriesBotDB"]

files_collection = db["files"]
state_collection = db["state"]
users_collection = db["users"]

# ---------------- BASIC USERS / FILES ----------------

async def add_user(uid: int):
    await asyncio.to_thread(lambda: users_collection.update_one(
        {"_id": uid}, {"$set": {"_id": uid}}, upsert=True
    ))

async def get_total_users():
    return await asyncio.to_thread(lambda: users_collection.count_documents({}))

async def get_total_files():
    return await asyncio.to_thread(lambda: files_collection.count_documents({}))

# ---------------- GLOBAL SETTINGS ----------------

async def get_global_setting(key, default=None):
    doc = await asyncio.to_thread(lambda: state_collection.find_one({"_id": key}))
    return doc.get("value", default) if doc else default

async def set_global_setting(key, value):
    await asyncio.to_thread(lambda: state_collection.update_one(
        {"_id": key}, {"$set": {"value": value}}, upsert=True
    ))

# ---------------- CONNECTED GROUPS ----------------

CONNECTED_GROUPS = "CONNECTED_GROUPS"

async def get_connected_groups():
    return await get_global_setting(CONNECTED_GROUPS, [])

async def add_connected_group(cid: int):
    g = await get_connected_groups()
    if cid not in g:
        g.append(cid)
        await set_global_setting(CONNECTED_GROUPS, g)

async def remove_connected_group(cid: int):
    g = await get_connected_groups()
    if cid in g:
        g.remove(cid)
        await set_global_setting(CONNECTED_GROUPS, g)

async def is_group_connected(cid: int):
    g = await get_connected_groups()
    return cid in g

# ---------------- GROUP SETTINGS ----------------

def _gkey(cid): return f"group_{cid}"

DEFAULT_GROUP = {
    "series": True,
    "imdb": True,
    "strict": False,
    "fsub": None,
    "autodel": 0
}

async def get_group_settings(cid: int):
    return await get_global_setting(_gkey(cid), DEFAULT_GROUP.copy())

async def set_group_settings(cid: int, data: dict):
    await set_global_setting(_gkey(cid), data)

async def toggle_group_setting(cid: int, key: str):
    s = await get_group_settings(cid)
    s[key] = not s.get(key, False)
    await set_group_settings(cid, s)
    return s

# ---------------- DASHBOARD ----------------

DASHBOARD_DEFAULT = {
    "global_imdb": True,
    "global_strict": False
}

async def get_dashboard_settings():
    return await get_global_setting("ADMIN_DASHBOARD", DASHBOARD_DEFAULT.copy())

async def toggle_dashboard_setting(key: str):
    s = await get_dashboard_settings()
    s[key] = not s.get(key, False)
    await set_global_setting("ADMIN_DASHBOARD", s)
    return s
