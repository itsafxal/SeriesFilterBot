# main.py — MULTI-DB EDITION + YES/NO RESUME PROMPT + OPTIMIZED STARTUP
# FIXES & OPTIMIZATIONS:
#   1. Bot ID cached once at startup (zero API calls in handlers)
#   2. Better error handling with graceful fallbacks
#   3. Concurrent background tasks for faster startup
#   4. Improved logging and exception handling
#   5. Safe shutdown with proper cleanup

import asyncio
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
    ensure_all_indexes, get_all_groups,
)
from config import API_ID, API_HASH, BOT_TOKEN, logger, ADMIN_USERS
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ── Cached bot identity (avoids get_me() on every event) ─────────
_BOT_ID: int | None = None
_BOT_USERNAME: str | None = None


# ─────────────────────────────────────────────────────────────
# INDEX RESUME — sends Yes/No prompt to admin
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


# ─────────────────────────────────────────────────────────────
# BOT STARTUP
# ─────────────────────────────────────────────────────────────

async def run_bot_async(cyber: Client):
    global _BOT_ID, _BOT_USERNAME

    logger.info("[STARTUP] Connecting bot client...")
    await cyber.start()
    logger.info("[STARTUP] Bot client connected ✅")

    # ── FIX: cache bot ID and username once to avoid get_me() on every event ──
    try:
        me = await cyber.get_me()
        _BOT_ID = me.id
        _BOT_USERNAME = me.username or ""
        logger.info(f"[STARTUP] ✅ Bot identity cached: @{_BOT_USERNAME} (id={_BOT_ID})")
    except Exception as e:
        logger.error(f"[STARTUP] Could not fetch bot identity: {e}")
        _BOT_ID = None
        _BOT_USERNAME = ""

    # Run all startup tasks concurrently for speed
    startup_tasks = [
        ensure_all_indexes(),
        load_fsub_settings(),
        repair_normalized_names_in_mongo(),
        check_and_resume_index(cyber),
    ]

    # Execute all startup tasks concurrently
    results = await asyncio.gather(*startup_tasks, return_exceptions=True)
    
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.warning(f"[STARTUP] Task {i} failed: {result}")

    # Start scheduler
    try:
        start_scheduler()
        logger.info("[STARTUP] ✅ Auto-delete scheduler started.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Auto-delete scheduler failed: {e}")

    # Clear sticker cache
    try:
        from DB.database import clear_sticker_cache
        await clear_sticker_cache()
        logger.info("[STARTUP] ✅ Sticker cache cleared (style reset).")
    except Exception as e:
        logger.warning(f"[STARTUP] Sticker cache clear failed (non-fatal): {e}")

    # Send startup notification to admin
    admin_id = ADMIN_USERS[0] if ADMIN_USERS else None
    if admin_id:
        try:
            await cyber.send_message(
                admin_id,
                "**🚀 Bot Restarted Successfully!**\n"
                "Multi-DB mode active (Config DB + File DB1 + File DB2).\n"
                "FSub settings loaded.",
                parse_mode=enums.ParseMode.MARKDOWN,
                disable_notification=True,
            )
            logger.info(f"[STARTUP] ✅ Restart notification sent to admin {admin_id}.")
        except Exception as e:
            logger.error(f"[STARTUP] Failed to send restart notification: {e}")

    logger.info("[STARTUP] 🟢 Bot is fully running.")

    try:
        await asyncio.Future()  # Run forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("[SHUTDOWN] Stopping bot...")

    await cyber.stop()
    logger.info("[SHUTDOWN] Bot stopped.")


# ─────────────────────────────────────────────────────────────
# GROUP TRACKING
# ─────────────────────────────────────────────────────────────

def _setup_group_tracking(cyber: Client):
    """Setup group tracking handler with optimizations."""
    from pyrogram import filters as _filters
    from pyrogram.types import ChatMemberUpdated

    @cyber.on_chat_member_updated()
    async def _track_groups(_, update: ChatMemberUpdated):
        try:
            chat = update.chat
            if not chat or chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
                return

            bot_id = _BOT_ID
            if bot_id is None:
                # Fallback: try to get ID if startup fetch failed
                try:
                    me = await cyber.get_me()
                    bot_id = me.id
                except Exception:
                    return

            new_member = update.new_chat_member
            old_member = update.old_chat_member

            # Identify if this event is about the bot itself
            new_is_bot = (
                new_member and new_member.user and new_member.user.id == bot_id
            )
            old_is_bot = (
                old_member and old_member.user and old_member.user.id == bot_id
            )

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

            chat_title = getattr(chat, "title", "") or ""
            chat_username = getattr(chat, "username", "") or ""

            # Bot was added / promoted to a group
            if new_is_bot and new_member.status in ACTIVE_STATUSES:
                old_status = old_member.status if old_member else None
                if old_status not in ACTIVE_STATUSES:
                    await add_group(chat.id, title=chat_title, username=chat_username)
                    logger.info(
                        f"[GROUP] ➕ Bot added to '{chat_title}' ({chat.id})"
                    )

            # Bot was removed / banned / left from a group
            elif old_is_bot and old_member.status in ACTIVE_STATUSES:
                new_status = new_member.status if new_member else None
                if new_status is None or new_status in GONE_STATUSES:
                    await remove_group(chat.id)
                    logger.info(
                        f"[GROUP] ➖ Bot removed from '{chat_title}' ({chat.id})"
                    )

        except Exception as e:
            logger.debug(f"[GROUP] tracking error: {e}")


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main():
    logger.info("[INIT] Initializing SQL database (migration from DB1 + DB2)...")
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
        # Discard all updates that accumulated while the bot was offline.
        skip_updates=True,
    )

    logger.info("[INIT] Registering handlers...")
    register_command_handlers(cyber)
    register_menu_handlers(cyber)
    register_filters(cyber)
    register_fsub_handlers(cyber)
    register_index_handlers(cyber)
    _setup_group_tracking(cyber)

    logger.info("[INIT] ✅ All handlers registered.")
    logger.info("[INIT] Starting bot event loop...")
    cyber.run(run_bot_async(cyber))


if __name__ == "__main__":
    main()
