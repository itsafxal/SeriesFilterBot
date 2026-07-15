import time
import asyncio
import re
from pyrogram import enums
from pyrogram.errors import FloodWait, BadRequest
from pyrogram.types import InputMediaDocument, InputMediaVideo, InputMediaAudio
from config import logger, STICKER_ENABLED, PROGRESS_MESSAGE_ENABLED
from DB.database import (
    get_all_files_by_season_and_quality,
    get_global_setting,
    config_db,
    update_file_id,
    get_cached_sticker,
    save_sticker_cache,
    get_sticker_style,
    save_sticker_style,
)
from utils.cyber_delete import delete_message_later
from utils.cyber_flood import retry_on_flood

# ── Constants ─────────────────────────────────────────────────
DEFAULT_CAPTION = (
    "<b>{series}</b>\n\n"
    "<b>{file_name}</b>\n\n"
    "Season <b>{season}</b> • Episode <b>{episode}</b>\n"
    "{language} • {quality}"
)
MEDIA_GROUP_SIZE = 10
SESSION_TTL = 600

# ── Session store ─────────────────────────────────────────────
_SESSION_MEM = {}
_sessions_col = config_db["user_sessions"]
try:
    _sessions_col.create_index("expires_at", expireAfterSeconds=0, background=True)
except Exception:
    pass

def set_session(user_id, data):
    from datetime import datetime, timedelta
    expires = time.time() + SESSION_TTL
    _SESSION_MEM[user_id] = {"data": data, "expires": expires}
    doc = {
        "_id": user_id,
        "data": data,
        "expires_at": datetime.utcnow() + timedelta(seconds=SESSION_TTL),
    }
    asyncio.create_task(
        asyncio.to_thread(
            lambda: _sessions_col.replace_one({"_id": user_id}, doc, upsert=True)
        )
    )

async def get_session_async(user_id):
    from datetime import datetime
    entry = _SESSION_MEM.get(user_id)
    if entry and time.time() < entry["expires"]:
        return entry["data"]
    def _sync():
        doc = _sessions_col.find_one({"_id": user_id})
        if not doc:
            return None
        if doc["expires_at"] < datetime.utcnow():
            _sessions_col.delete_one({"_id": user_id})
            return None
        return doc["data"]
    return await asyncio.to_thread(_sync)

def clear_session(user_id):
    _SESSION_MEM.pop(user_id, None)
    asyncio.create_task(
        asyncio.to_thread(lambda: _sessions_col.delete_one({"_id": user_id}))
    )

# ── Helpers ────────────────────────────────────────────────────
def caption_filename(name):
    name = re.sub(r"\.(mkv|mp4|avi)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[._\-]+", " ", name)
    return name.strip()

# ── Caption template cache (TTL 60s) ──────────────────────────
_caption_template_cache: str | None = None
_caption_cache_ts: float = 0.0
_CAPTION_CACHE_TTL: float = 60.0

def _invalidate_caption_cache():
    global _caption_cache_ts
    _caption_cache_ts = 0.0

async def _get_caption_template() -> str:
    global _caption_template_cache, _caption_cache_ts
    if _caption_template_cache is not None and (time.time() - _caption_cache_ts) < _CAPTION_CACHE_TTL:
        return _caption_template_cache
    raw = await get_global_setting("CUSTOM_FILE_CAPTION", None)
    if not raw or not isinstance(raw, str) or not raw.strip():
        raw = await get_global_setting("FILE_CAPTION", None)
    if raw and isinstance(raw, str) and raw.strip():
        _caption_template_cache = raw.strip()
        logger.info(f"[CAPTION] Loaded custom template: {_caption_template_cache!r}")
    else:
        _caption_template_cache = DEFAULT_CAPTION
        logger.info("[CAPTION] No custom caption in DB — using DEFAULT_CAPTION.")
    _caption_cache_ts = time.time()
    return _caption_template_cache

def build_caption_sync(template, series, season, episode, language, quality, file_name):
    vars_ = dict(
        series=series,
        file_name=caption_filename(file_name),
        season=str(season).zfill(2),
        episode=str(episode).zfill(2),
        language=language,
        quality=quality,
    )
    try:
        return template.format_map(vars_)
    except (KeyError, ValueError) as exc:
        logger.warning(f"[CAPTION] Invalid placeholder in custom template ({exc}) — falling back")
        _invalidate_caption_cache()
        return DEFAULT_CAPTION.format_map(vars_)

async def build_caption(series, season, episode, language, quality, file_name):
    template = await _get_caption_template()
    return build_caption_sync(template, series, season, episode, language, quality, file_name)

def make_media(f, caption):
    file_id = f["file_id"]
    t = f.get("media_type", "document")
    if t == "video":
        return InputMediaVideo(file_id, caption=caption, parse_mode=enums.ParseMode.HTML)
    if t == "audio":
        return InputMediaAudio(file_id, caption=caption, parse_mode=enums.ParseMode.HTML)
    return InputMediaDocument(file_id, caption=caption, parse_mode=enums.ParseMode.HTML)

async def safe_send(cyber, user_id, f, caption, auto_delay):
    """Send correct media type (video, audio, or document) with flood retry; returns False on BadRequest / fatal errors."""
    media_type = f.get("media_type", "document")
    if media_type == "video":
        send_fn = lambda: cyber.send_video(
            user_id, f["file_id"],
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )
    elif media_type == "audio":
        send_fn = lambda: cyber.send_audio(
            user_id, f["file_id"],
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        send_fn = lambda: cyber.send_document(
            user_id, f["file_id"],
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        sent = await retry_on_flood(
            send_fn,
            label="files.send", max_retries=5,
        )
        if auto_delay:
            delete_message_later(cyber, user_id, sent.id, auto_delay)
        return True
    except BadRequest:
        return False
    except Exception as e:
        logger.error(f"safe_send error: {e}")
        return False

# ── Sticker helpers ────────────────────────────────────────────
async def _send_start_sticker(cyber, user_id: int, season: int, auto_delay: int) -> int | None:
    try:
        from utils.cyber_sticker import make_start_sticker
        style_idx = await get_sticker_style(season, "start")
        buf, chosen_style = make_start_sticker(season, style_idx=style_idx)
        if style_idx is None:
            await save_sticker_style(season, "start", chosen_style)
        sent = await cyber.send_sticker(user_id, buf)
        await save_sticker_cache(season, sent.sticker.file_id, kind="start")
        if auto_delay:
            delete_message_later(cyber, user_id, sent.id, auto_delay)
        return sent.id
    except Exception as e:
        logger.warning(f"[STICKER] Failed to send START sticker: {e}")
        return None

async def _send_end_sticker(cyber, user_id: int, season: int, total: int, auto_delay: int) -> int | None:
    try:
        from utils.cyber_sticker import make_end_sticker
        style_idx = await get_sticker_style(season, "end")
        buf, _ = make_end_sticker(season, total, style_idx=style_idx)
        if style_idx is None:
            await save_sticker_style(season, "end", 0)  # only one style
        sent = await cyber.send_sticker(user_id, buf)
        await save_sticker_cache(season, sent.sticker.file_id, kind="end")
        if auto_delay:
            delete_message_later(cyber, user_id, sent.id, auto_delay)
        return sent.id
    except Exception as e:
        logger.warning(f"[STICKER] Failed to send END sticker: {e}")
        return None

# ── Main delivery ─────────────────────────────────────────────
async def deliver_files_direct(cyber, user_id, on_complete=None):
    session = await get_session_async(user_id)
    if not session:
        if on_complete:
            await on_complete()
        return
    series = session["series"]
    season = int(session["season"])
    language = session["language"]
    quality = session["quality"]
    files = await get_all_files_by_season_and_quality(series.lower(), season, quality, language)
    if not files:
        await cyber.send_message(user_id, "❌ No files found.")
        clear_session(user_id)
        if on_complete:
            await on_complete()
        return
    files.sort(key=lambda x: int(x.get("episode", 0)))
    auto_delay = int(await get_global_setting("FILE_AUTODEL_DELAY", 0) or 0)
    clear_session(user_id)

    if STICKER_ENABLED:
        await _send_start_sticker(cyber, user_id, season, auto_delay)

    if PROGRESS_MESSAGE_ENABLED:
        msg = await cyber.send_message(user_id, f"📦 Sending {len(files)} files...")

    sent = 0
    for i in range(0, len(files), MEDIA_GROUP_SIZE):
        batch = files[i:i + MEDIA_GROUP_SIZE]
        if len(batch) == 1:
            f = batch[0]
            cap = await build_caption(series, season, f["episode"], language, quality, f["file_name"])
            if await safe_send(cyber, user_id, f, cap, auto_delay):
                sent += 1
        else:
            media = []
            for f in batch:
                cap = await build_caption(series, season, f["episode"], language, quality, f["file_name"])
                media.append(make_media(f, cap))
            try:
                msgs = await cyber.send_media_group(user_id, media)
                sent += len(msgs)
                if auto_delay:
                    for m in msgs:
                        delete_message_later(cyber, user_id, m.id, auto_delay)
            except Exception as e:
                logger.error(f"Group failed – fallback: {e}")
                for f in batch:
                    cap = await build_caption(series, season, f["episode"], language, quality, f["file_name"])
                    if await safe_send(cyber, user_id, f, cap, auto_delay):
                        sent += 1

    if PROGRESS_MESSAGE_ENABLED:
        await cyber.delete_messages(user_id, msg.id)

    if STICKER_ENABLED:
        await _send_end_sticker(cyber, user_id, season, sent, auto_delay)

    if auto_delay:
        notif = await cyber.send_message(user_id, f"⏳ Files will auto-delete in {auto_delay}s")
        delete_message_later(cyber, user_id, notif.id, auto_delay)

    logger.info(f"✅ Delivered {sent}/{len(files)} to {user_id}")
    if on_complete:
        await on_complete()
