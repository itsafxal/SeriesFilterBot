from pyrogram import enums, types
from DB.database import save_file_to_db, get_all_files_by_season_and_quality
from utils.series_helper import parse_series_info
from CyberTG.poster import get_series_poster_async
from config import DEFAULT_POSTER, logger


async def save_series_file_for_bulk_index(message: types.Message):

    file_name = ""
    file_id = None

    if message.document:
        file_name = message.document.file_name or ""
        file_id = message.document.file_id

    elif message.video:
        file_name = message.video.file_name or ""
        file_id = message.video.file_id

    if not file_name:
        return False, 0

    series, season, episode, quality, language = parse_series_info(file_name)

    if season == 0:
        return False, 0

    existing = await get_all_files_by_season_and_quality(series, season, quality, language)
    for r in existing:
        if r["episode"] == episode:
            return False, 0

    poster = await get_series_poster_async(series) or DEFAULT_POSTER

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
        "poster": poster,
        "timestamp": message.date
    }

    try:
        await save_file_to_db(doc)
        return True, 1
    except Exception as e:
        logger.error(e)
        return False, 0
