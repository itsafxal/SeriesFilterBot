from pyrogram import enums, types
from DB.database import save_file_to_db, get_all_files_by_season_and_quality
from utils.cyber_series import parse_series_info
from CyberTG.poster import get_series_info
from config import DEFAULT_POSTER, logger

# 🔥 Cache to avoid repeated API calls per series
SERIES_LANGUAGE_CACHE = {}

async def save_series_file_for_bulk_index(message: types.Message):

    file_name = ""
    file_id = None
    media_type = None

    # ------------------ MEDIA DETECTION ------------------

    if message.document:
        file_name = message.document.file_name or ""
        file_id = message.document.file_id
        media_type = "document"

    elif message.video:
        file_name = message.video.file_name or ""
        file_id = message.video.file_id
        media_type = "video"

    elif message.audio:
        file_name = message.audio.file_name or ""
        file_id = message.audio.file_id
        media_type = "audio"

    else:
        return False, 0

    if not file_name:
        return False, 0

    # ------------------ PARSE SERIES INFO ------------------

    series, season, episode, quality, language = parse_series_info(file_name)

    if season == 0:
        return False, 0

    # ------------------ AUTO LANGUAGE DETECT (ONLY IF UNKNOWN) ------------------

    if language in ["Unknown", "Original Language", "", None]:

        try:
            # Use cache first
            if series in SERIES_LANGUAGE_CACHE:
                language = SERIES_LANGUAGE_CACHE[series]

            else:
                poster, year, genre, rating = await get_series_info(series)

                # get_series_info already uses TMDB internally
                # We assume TMDB mapping sets proper language
                from CyberTG.poster import fetch_tmdb

                tmdb_data = await fetch_tmdb(series)

                if tmdb_data:
                    _, _, _, _, api_language = tmdb_data
                    if api_language:
                        language = api_language
                        SERIES_LANGUAGE_CACHE[series] = api_language

        except Exception as e:
            logger.error(f"Language auto-detect failed: {e}")

    # ------------------ DUPLICATE CHECK ------------------

    existing = await get_all_files_by_season_and_quality(
        series, season, quality, language
    )

    for r in existing:
        if r["episode"] == episode:
            return False, 0

    # ------------------ POSTER ------------------

    poster, _, _, _ = await get_series_info(series)
    poster = poster or DEFAULT_POSTER

    # ------------------ SAVE DOC ------------------

    doc = {
        "channel_id": message.chat.id,
        "message_id": message.id,
        "file_name": file_name,
        "normalized_name": series,
        "season": season,
        "episode": episode,
        "quality": quality,
        "language": language,
        "file_id": file_id,
        "media_type": media_type,
        "poster": poster,
        "timestamp": message.date
    }

    try:
        await save_file_to_db(doc)
        return True, 1
    except Exception as e:
        logger.error(e)
        return False, 0
