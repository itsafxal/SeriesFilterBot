# main.py — FIXED
# Key fix: register_fsub_handlers(cyber) is now called BEFORE cyber.start()
# This ensures the chat_join_request handler is bound to the client instance
# and Pyrogram routes join request updates to it correctly.

import asyncio
from pyrogram import Client, enums
from CyberTG.command import register_command_handlers
from CyberTG.menus import register_menu_handlers
from CyberTG.index import index_files_task, lock, INDEX_STATUS_KEY, register_index_handlers
from CyberTG.filters import register_filters
from CyberTG.fsub import load_fsub_settings, register_fsub_handlers   # ← added register_fsub_handlers
from utils.cyber_delete import start_scheduler
from DB.database import initialize_sql_db, get_global_setting, set_global_setting
from config import API_ID, API_HASH, BOT_TOKEN, logger, ADMIN_USERS
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


# ─────────────────────────────────────────────────────────────
# INDEX RESUME
# ─────────────────────────────────────────────────────────────

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
        if len(parts) != 4:
            raise ValueError(f"Unexpected status format: {status!r}")

        _, chat_id_str, start_msg_id_str, last_msg_id_str = parts
        chat_id      = int(chat_id_str)
        start_msg_id = int(start_msg_id_str)
        last_msg_id  = int(last_msg_id_str)

        logger.warning(
            f"[STARTUP] Resuming interrupted index for chat={chat_id} "
            f"from checkpoint {start_msg_id} → {last_msg_id}"
        )

        admin_id = ADMIN_USERS[0] if ADMIN_USERS else None

        if not admin_id:
            asyncio.create_task(
                index_files_task(cyber, None, chat_id, start_msg_id, last_msg_id)
            )
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Indexing", callback_data="cancel_index")]
        ])

        try:
            msg = await cyber.send_message(
                admin_id,
                f"**♻️ [RESUMED INDEX]**\n"
                f"Indexing resumed for channel `{chat_id}`.\n"
                f"Resuming from message ID **{start_msg_id}** → **{last_msg_id}**.\n\n"
                f"Press cancel to stop.",
                reply_markup=kb,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"[STARTUP] Could not send resume notification to admin: {e}")
            msg = None

        asyncio.create_task(
            index_files_task(cyber, msg, chat_id, start_msg_id, last_msg_id)
        )
        logger.info("[STARTUP] Index resumption task started successfully.")

    except Exception as e:
        logger.error(f"[STARTUP] Failed to resume index task: {e} — clearing status.")
        try:
            await set_global_setting(INDEX_STATUS_KEY, "IDLE")
        except Exception as e2:
            logger.error(f"[STARTUP] Failed to clear INDEX_STATUS_KEY: {e2}")


# ─────────────────────────────────────────────────────────────
# BOT STARTUP
# ─────────────────────────────────────────────────────────────

async def run_bot_async(cyber: Client):
    logger.info("[STARTUP] Connecting bot client...")
    await cyber.start()
    logger.info("[STARTUP] Bot client connected ✅")

    # 1. Start auto-delete scheduler
    try:
        start_scheduler()
        logger.info("[STARTUP] ✅ Auto-delete scheduler started.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Auto-delete scheduler failed: {e}")

    # 2. Load FSub settings
    try:
        await load_fsub_settings()
        logger.info("[STARTUP] ✅ FSub settings loaded.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Failed to load FSub settings: {e}")

    # 3. Register index handlers
    try:
        register_index_handlers(cyber)
        logger.info("[STARTUP] ✅ Index handlers registered.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Failed to register index handlers: {e}")

    # 4. Resume any interrupted index task
    try:
        asyncio.create_task(check_and_resume_index(cyber))
        logger.info("[STARTUP] ✅ Index resume check scheduled.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Failed to schedule index resume check: {e}")

    # 5. Notify admin
    admin_id = ADMIN_USERS[0] if ADMIN_USERS else None
    if admin_id:
        try:
            await cyber.send_message(
                admin_id,
                "**🚀 Bot Restarted Successfully!**\n"
                "All systems nominal. FSub settings loaded.",
                parse_mode=enums.ParseMode.MARKDOWN
            )
            logger.info(f"[STARTUP] ✅ Restart notification sent to admin {admin_id}.")
        except Exception as e:
            logger.error(f"[STARTUP] Failed to send restart notification: {e}")

    logger.info("[STARTUP] 🟢 Bot is fully running. Waiting for updates...")

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("[SHUTDOWN] Stopping bot...")

    await cyber.stop()
    logger.info("[SHUTDOWN] Bot stopped.")


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main():
    logger.info("[INIT] Initializing SQL database...")
    try:
        initialize_sql_db()
        logger.info("[INIT] ✅ SQL database is ready.")
    except Exception as e:
        logger.error(f"[INIT] ❌ Critical failure during SQL DB init: {e}")
        return

    cyber = Client(
        "SeriesBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
    )

    # Register all handlers BEFORE starting
    logger.info("[INIT] Registering handlers...")
    register_command_handlers(cyber)
    register_menu_handlers(cyber)
    register_filters(cyber)

    # ✅ THE KEY FIX: register FSub join request handler on the instance
    # This MUST be called before cyber.start() so Pyrogram binds it correctly.
    register_fsub_handlers(cyber)

    logger.info("[INIT] ✅ All handlers registered.")

    logger.info("[INIT] Starting bot event loop...")
    cyber.run(run_bot_async(cyber))


if __name__ == "__main__":
    main()
