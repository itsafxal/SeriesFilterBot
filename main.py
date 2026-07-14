import asyncio
import signal
from pyrogram import Client, enums
from CyberTG.command import register_command_handlers
from CyberTG.menus import register_menu_handlers
from CyberTG.index import index_files_task, lock, INDEX_STATUS_KEY, register_index_handlers
from CyberTG.filters import register_filters
from CyberTG.fsub import load_fsub_settings, register_fsub_handlers
from utils.cyber_delete import start_scheduler
from DB.database import (
    initialize_sql_db, get_global_setting, set_global_setting,
    repair_normalized_names_in_mongo, add_group, remove_group,
    ensure_all_indexes,
)
from config import API_ID, API_HASH, BOT_TOKEN, logger, ADMIN_USERS
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

_BOT_ID: int | None = None
_BOT_USERNAME: str | None = None

# ── Graceful shutdown event ────────────────────────────────────
_shutdown_event = asyncio.Event()

async def check_and_resume_index(cyber: Client):
    if lock.locked():
        logger.warning("[STARTUP] Index lock is held — skipping resume check.")
        return
    try:
        status = await get_global_setting(INDEX_STATUS_KEY, "IDLE")
    except Exception as e:
        logger.error(f"[STARTUP] Failed to read INDEX_STATUS_KEY: {e}")
        return
    if not isinstance(status, str) or not status.startswith("RUNNING"):
        logger.info("[STARTUP] No interrupted index task found.")
        return
    try:
        parts = status.split("|")
        if len(parts) == 4:
            _, chat_id_str, start_msg_id_str, last_msg_id_str = parts
            target = "both"
        elif len(parts) == 5:
            _, chat_id_str, start_msg_id_str, last_msg_id_str, target = parts
        else:
            raise ValueError(f"Unexpected status format: {status!r}")
        chat_id = int(chat_id_str)
        start_msg_id = int(start_msg_id_str)
        last_msg_id = int(last_msg_id_str)
        label = {"db1": "DB1", "db2": "DB2", "both": "DB1 + DB2"}.get(target, target)
        logger.warning(
            f"[STARTUP] Found interrupted index for chat={chat_id} "
            f"range {start_msg_id}→{last_msg_id} | target={label}"
        )
        admin_id = ADMIN_USERS[0] if ADMIN_USERS else None
        if not admin_id:
            logger.warning("[STARTUP] No admin configured — auto-resuming index.")
            asyncio.create_task(
                index_files_task(cyber, None, chat_id, start_msg_id, last_msg_id, target=target)
            )
            return
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "▶️ Yes, Resume",
                    callback_data=f"resume_yes|{chat_id}|{start_msg_id}|{last_msg_id}|{target}",
                ),
                InlineKeyboardButton(
                    "🗑 No, Discard",
                    callback_data="resume_no",
                ),
            ]
        ])
        try:
            await cyber.send_message(
                admin_id,
                f"♻️ **Interrupted Index Detected!**\n\n"
                f"The bot was restarted while an index was running.\n\n"
                f"📡 **Channel** : `{chat_id}`\n"
                f"📌 **Range**   : `{start_msg_id}` → `{last_msg_id}`\n"
                f"🎯 **Target**  : `{label}`\n\n"
                f"Do you want to **resume** from the last checkpoint?",
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=kb,
            )
            logger.info(f"[STARTUP] Resume prompt sent to admin {admin_id}.")
        except Exception as e:
            logger.error(f"[STARTUP] Could not send resume prompt to admin: {e}")
            asyncio.create_task(
                index_files_task(cyber, None, chat_id, start_msg_id, last_msg_id, target=target)
            )
    except Exception as e:
        logger.error(f"[STARTUP] Failed to parse interrupted index state: {e} — clearing status.")
        try:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        except Exception as e2:
            logger.error(f"[STARTUP] Failed to clear INDEX_STATUS_KEY: {e2}")

async def run_bot_async(cyber: Client):
    global _BOT_ID, _BOT_USERNAME
    logger.info("[STARTUP] Connecting bot client...")
    await cyber.start()
    logger.info("[STARTUP] Bot client connected ✅")

    # ── Global asyncio exception handler — catches silent failures in background tasks ──
    loop = asyncio.get_running_loop()
    def _global_exception_handler(loop, context):
        exception = context.get('exception')
        message = context.get('message', '') if isinstance(context, dict) else str(context)
        logger.error(f"[ASYNCIO] Unhandled exception: {message}")
        if exception:
            logger.exception(f"[ASYNCIO] Traceback", exc_info=(type(exception), exception, exception.__traceback__))
    loop.set_exception_handler(_global_exception_handler)
    logger.info("[STARTUP] Global asyncio exception handler installed.")

    try:
        me = await cyber.get_me()
        _BOT_ID = me.id
        _BOT_USERNAME = me.username or ""
        import config as cfg
        cfg.bot_id = _BOT_ID
        cfg.bot_username = _BOT_USERNAME
        cfg.bot_first_name = me.first_name or ""
        logger.info(f"[STARTUP] ✅ Bot identity cached: @{_BOT_USERNAME} (id={_BOT_ID})")
    except Exception as e:
        logger.error(f"[STARTUP] Could not fetch bot identity: {e}")
    startup_tasks = [
        ensure_all_indexes(),
        load_fsub_settings(),
        repair_normalized_names_in_mongo(),
        check_and_resume_index(cyber),
    ]
    results = await asyncio.gather(*startup_tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.warning(f"[STARTUP] Task {i} failed: {result}")
    try:
        start_scheduler()
        logger.info("[STARTUP] ✅ Auto-delete scheduler started.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Auto-delete scheduler failed: {e}")
    try:
        from DB.database import clear_sticker_cache
        await clear_sticker_cache()
        logger.info("[STARTUP] ✅ Sticker cache cleared.")
    except Exception as e:
        logger.warning(f"[STARTUP] Sticker cache clear failed: {e}")
    admin_id = ADMIN_USERS[0] if ADMIN_USERS else None
    if admin_id:
        try:
            await cyber.send_message(
                admin_id,
                "🚀 Bot restarted successfully.\nMulti‑DB mode active.",
                disable_notification=True,
            )
        except Exception as e:
            logger.error(f"[STARTUP] Admin notification failed: {e}")
    logger.info("[STARTUP] Bot fully running")
    try:
        await _shutdown_event.wait()
    except asyncio.CancelledError:
        logger.info("[SHUTDOWN] Received stop signal. Shutting down gracefully...")
    except Exception as e:
        logger.error(f"[SHUTDOWN] Unexpected error: {e}")
    finally:
        logger.info("[SHUTDOWN] Stopping bot...")
        await cyber.stop()
        try:
            from DB.database import config_client, file_client_1, file_client_2
            config_client.close()
            file_client_1.close()
            file_client_2.close()
            logger.info("[SHUTDOWN] ✅ Database connections closed.")
        except Exception as e:
            logger.error(f"[SHUTDOWN] Error closing DBs: {e}")
        logger.info("[SHUTDOWN] ✅ Graceful shutdown complete.")

def _setup_group_tracking(cyber: Client):
    from pyrogram import filters as _filters
    from pyrogram.types import ChatMemberUpdated, InlineKeyboardMarkup, InlineKeyboardButton
    @cyber.on_message(_filters.new_chat_members & _filters.group)
    async def _bot_added_to_group(_, message):
        try:
            bot_id = _BOT_ID
            if bot_id is None:
                return
            new_ids = [u.id for u in (message.new_chat_members or [])]
            if bot_id not in new_ids:
                return
            chat = message.chat
            chat_title = getattr(chat, "title", "") or "Unknown Group"
            chat_id = chat.id
            try:
                await add_group(chat_id, title=chat_title, username=getattr(chat, "username", "") or "")
                logger.info(f"[GROUP] ➕ Bot added to '{chat_title}' ({chat_id})")
            except Exception as e:
                logger.error(f"[GROUP] Failed to add group {chat_id}: {e}")
            try:
                await message.reply_text(
                    f"<b>👋 Thanks for adding me to {chat_title}!</b>\n"
                    f"⚡ Make me Admin for full functionality.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("➕ Add me to your group",
                                             url=f"https://t.me/{_BOT_USERNAME}?startgroup=start")
                    ]]),
                )
            except Exception as e:
                logger.warning(f"[GROUP] Could not send welcome message: {e}")
            admin_id = ADMIN_USERS[0] if ADMIN_USERS else None
            if admin_id:
                try:
                    total = await cyber.get_chat_members_count(chat_id)
                except Exception:
                    total = "N/A"
                try:
                    await cyber.send_message(
                        admin_id,
                        f"<b>📥 Bot Added to a Group</b>\n\n"
                        f"🏷 <b>Title :</b> {chat_title}\n"
                        f"🆔 <b>ID    :</b> <code>{chat_id}</code>\n"
                        f"👥 <b>Members :</b> {total}",
                    )
                except Exception as e:
                    logger.warning(f"[GROUP] Could not send log to admin: {e}")
        except Exception as e:
            logger.debug(f"[GROUP] _bot_added_to_group error: {e}")
    @cyber.on_chat_member_updated()
    async def _bot_left_group(_, update: ChatMemberUpdated):
        try:
            chat = update.chat
            if not chat or chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
                return
            bot_id = _BOT_ID
            if bot_id is None:
                return
            old_member = update.old_chat_member
            new_member = update.new_chat_member
            old_is_bot = old_member and old_member.user and old_member.user.id == bot_id
            ACTIVE_STATUSES = (
                enums.ChatMemberStatus.MEMBER,
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            )
            GONE_STATUSES = (
                enums.ChatMemberStatus.LEFT,
                enums.ChatMemberStatus.BANNED,
                enums.ChatMemberStatus.RESTRICTED,
            )
            if old_is_bot and old_member.status in ACTIVE_STATUSES:
                new_status = new_member.status if new_member else None
                if new_status is None or new_status in GONE_STATUSES:
                    chat_title = getattr(chat, "title", "") or ""
                    try:
                        await remove_group(chat.id)
                        logger.info(f"[GROUP] ➖ Bot removed from '{chat_title}' ({chat.id})")
                    except Exception as e:
                        logger.error(f"[GROUP] Failed to remove group {chat.id}: {e}")
        except Exception as e:
            logger.debug(f"[GROUP] _bot_left_group error: {e}")

async def main():
    if not all((API_ID, API_HASH, BOT_TOKEN)):
        logger.error("Missing required environment variables")
        return
    logger.info("[INIT] Initializing SQL database...")
    try:
        initialize_sql_db()
        logger.info("[INIT] ✅ SQL database is ready.")
    except Exception as e:
        logger.error(f"[INIT] ❌ Critical failure during SQL DB init: {e}")
        return
    cyber = Client("SeriesBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, skip_updates=True)
    logger.info("[INIT] Registering handlers...")
    register_command_handlers(cyber)
    register_menu_handlers(cyber)
    register_filters(cyber)
    register_fsub_handlers(cyber)
    register_index_handlers(cyber)
    _setup_group_tracking(cyber)
    logger.info("[INIT] ✅ All handlers registered.")
    
    # ── Signal handling for Docker / systemd graceful shutdown ──
    def _signal_handler(signum, frame):
        logger.info(f"[SHUTDOWN] Received signal {signum} — triggering graceful shutdown.")
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT,  _signal_handler)

    # Run the bot with graceful exit handling
    try:
        await run_bot_async(cyber)
    except KeyboardInterrupt:
        logger.info("[SHUTDOWN] KeyboardInterrupt received. Exiting.")
    except Exception as e:
        logger.exception(f"[FATAL] Bot crashed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
