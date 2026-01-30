import time

SESSION_TTL = 600  # 10 minutes
USER_SESSIONS = {}


def set_session(user_id: int, data: dict):
    USER_SESSIONS[user_id] = {
        "data": data,
        "expires": time.time() + SESSION_TTL
    }


def get_session(user_id: int):
    s = USER_SESSIONS.get(user_id)
    if not s:
        return None

    if time.time() > s["expires"]:
        USER_SESSIONS.pop(user_id, None)
        return None

    return s["data"]


def clear_session(user_id: int):
    USER_SESSIONS.pop(user_id, None)
