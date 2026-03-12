# main.py — MULTI-DB EDITION + YES/NO RESUME PROMPT
# Changes:
#   - check_and_resume_index now sends admin a Yes/No prompt instead of auto-resuming
#   - Resume callbacks (resume_yes | resume_no) are handled in index.py handlers

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
    ensure_all_indexes,
)
from config import API_ID, API_HASH, BOT_TOKEN, logger, ADMIN_USERS
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


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

        # Support both old 4-part format and new 5-part format (with target)
        if len(parts) == 4:
            _, chat_id_str, start_msg_id_str, last_msg_id_str = parts
            target = "both"
        elif len(parts) == 5:
            _, chat_id_str, start_msg_id_str, last_msg_id_str, target = parts
        else:
            raise ValueError(f"Unexpected status format: {status!r}")

        chat_id      = int(chat_id_str)
        start_msg_id = int(start_msg_id_str)
        last_msg_id  = int(last_msg_id_str)

        label = {"db1": "DB1", "db2": "DB2", "both": "DB1 + DB2"}.get(target, target)
        logger.warning(
            f"[STARTUP] Found interrupted index for chat={chat_id} "
            f"range {start_msg_id}→{last_msg_id} | target={label}"
        )

        admin_id = ADMIN_USERS[0] if ADMIN_USERS else None

        if not admin_id:
            # No admin to ask — auto-resume silently as fallback
            logger.warning("[STARTUP] No admin configured — auto-resuming index.")
            asyncio.create_task(
                index_files_task(cyber, None, chat_id, start_msg_id, last_msg_id, target=target)
            )
            return

        # ── Send Yes/No prompt ─────────────────────────────────
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
            # Fall back to auto-resume if we can't reach admin
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
    logger.info("[STARTUP] Connecting bot client...")
    await cyber.start()
    logger.info("[STARTUP] Bot client connected ✅")

    # ── MongoDB indexes — run in background, don't block startup ──
    asyncio.create_task(ensure_all_indexes())
    logger.info("[STARTUP] ✅ MongoDB index check scheduled (background).")

    # ── Sticker cache clear — fire-and-forget, non-blocking ──
    async def _clear_stickers_bg():
        try:
            from DB.database import clear_sticker_cache
            await clear_sticker_cache()
            logger.info("[STARTUP] ✅ Sticker cache cleared (style reset).")
        except Exception as e:
            logger.warning(f"[STARTUP] Sticker cache clear failed (non-fatal): {e}")
    asyncio.create_task(_clear_stickers_bg())

    try:
        start_scheduler()
        logger.info("[STARTUP] ✅ Auto-delete scheduler started.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Auto-delete scheduler failed: {e}")

    try:
        await load_fsub_settings()
        logger.info("[STARTUP] ✅ FSub settings loaded.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Failed to load FSub settings: {e}")

    try:
        from utils.cyber_filebot import load_filebot_config, register_filebot_start_handler, is_filebot_configured
        await load_filebot_config()
        logger.info("[STARTUP] ✅ File bot config loaded.")
        if is_filebot_configured():
            register_filebot_start_handler(cyber)
            logger.info("[STARTUP] ✅ File bot /start handler scheduled.")
    except Exception as e:
        logger.warning(f"[STARTUP] File bot config load failed (non-fatal): {e}")

    # ── Mongo normalized_name repair — background, non-blocking ──
    async def _mongo_repair_bg():
        try:
            total = await repair_normalized_names_in_mongo()
            if total:
                logger.info(f"[STARTUP] ✅ MongoDB repair done — {total} docs updated.")
            else:
                logger.info("[STARTUP] ✅ MongoDB repair done — nothing to update.")
        except Exception as e:
            logger.warning(f"[STARTUP] MongoDB repair failed (non-fatal): {e}")
    asyncio.create_task(_mongo_repair_bg())
    logger.info("[STARTUP] ✅ MongoDB repair scheduled (background).")

    try:
        asyncio.create_task(check_and_resume_index(cyber))
        logger.info("[STARTUP] ✅ Index resume check scheduled.")
    except Exception as e:
        logger.exception(f"[STARTUP] ❌ Failed to schedule index resume check: {e}")

    admin_id = ADMIN_USERS[0] if ADMIN_USERS else None
    if admin_id:
        try:
            await cyber.send_message(
                admin_id,
                "**🚀 Bot Restarted Successfully!**\n"
                "Multi-DB mode active (Config DB + File DB1 + File DB2).\n"
                "FSub settings loaded.",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            logger.info(f"[STARTUP] ✅ Restart notification sent to admin {admin_id}.")
        except Exception as e:
            logger.error(f"[STARTUP] Failed to send restart notification: {e}")

    logger.info("[STARTUP] 🟢 Bot is fully running.")

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
    logger.info("[INIT] Initializing SQL database (migration from DB1 + DB2)...")
    try:
        initialize_sql_db()
        logger.info("[INIT] ✅ SQL database is ready.")
    except Exception as e:
        logger.error(f"[INIT] ❌ Critical failure during SQL DB init: {e}")
        return

    # Mongo normalized_name repair — deferred to background after bot is online.
    # This used to stream TWO full Mongo collections synchronously before the
    # event loop started — could take 30–120s on large DBs. Now it runs as a
    # background task after startup, so the bot is immediately responsive.
    # (The repair is idempotent; safe to skip on any individual boot.)

    cyber = Client(
        "SeriesBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
    )

    logger.info("[INIT] Registering handlers...")
    register_command_handlers(cyber)
    register_menu_handlers(cyber)
    register_filters(cyber)
    register_fsub_handlers(cyber)
    register_index_handlers(cyber)

    # ── Group tracking — keeps /stats group count accurate ──────
    from pyrogram import filters as _filters
    from pyrogram.types import ChatMemberUpdated

    @cyber.on_chat_member_updated()
    async def _track_groups(_, update: ChatMemberUpdated):
        try:
            chat = update.chat
            if not chat or chat.type not in ("group", "supergroup"):
                return
            me = await cyber.get_me()
            new_member = update.new_chat_member
            old_member = update.old_chat_member
            # Bot was added to a group
            if (new_member and new_member.user.id == me.id and
                    new_member.status in ("member", "administrator")):
                await add_group(chat.id)
            # Bot was removed from a group
            elif (old_member and old_member.user.id == me.id and
                  old_member.status in ("member", "administrator") and
                  (new_member is None or new_member.status in ("left", "kicked", "banned"))):
                await remove_group(chat.id)
        except Exception as e:
            logger.debug(f"[GROUP] tracking error: {e}")

    logger.info("[INIT] ✅ All handlers registered.")

    logger.info("[INIT] Starting bot event loop...")
    cyber.run(run_bot_async(cyber))


if __name__ == "__main__":
    main()
