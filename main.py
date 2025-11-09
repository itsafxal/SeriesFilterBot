
# main.py - FINAL WORKING IMPLEMENTATION without app.idle()

import asyncio
from pyrogram import Client
from pyrogram.errors import FloodWait
from command import register_command_handlers
from conversation import register_conversation_handlers, SERIES_CACHE, SERIES_CACHE_LC
# Ensure get_all_users is imported from database
from database import initialize_sql_db, get_all_users, search_series
from config import API_ID, API_HASH, BOT_TOKEN, logger

# --- Notification Function ---
async def notify_users_of_restart(client: Client):
    """Sends a restart notification message to all users in the database."""
    
    # Wait briefly to ensure the client is fully ready before sending broadcast
    await asyncio.sleep(5) 

    user_ids = await get_all_users()
    logger.info(f"Attempting to send restart message to {len(user_ids)} users...")
    
    restart_message = "🤖 Bot has just been restarted and is now fully operational! Thanks for your patience."
    success_count = 0
    failure_count = 0

    for user_id in user_ids:
        try:
            # Send message
            await client.send_message(
                chat_id=user_id,
                text=restart_message
                #parse_mode="markdownv2"
            )
            success_count += 1
            await asyncio.sleep(0.05) # Rate limit protection
        except FloodWait as e:
            logger.warning(f"FloodWait hit. Waiting for {e.value} seconds...")
            await asyncio.sleep(e.value)
            # Retrying for the current user after the wait
            try:
                await client.send_message(
                    chat_id=user_id,
                    text=restart_message
                    #parse_mode="markdownv2"
                )
                success_count += 1
                await asyncio.sleep(0.05)
            except Exception as e_retry:
                logger.error(f"Failed to send restart message to user {user_id} after retry: {e_retry}")
                failure_count += 1
        except Exception as e:
            # Catching blocked users, deleted accounts, etc.
            logger.error(f"Failed to send restart message to user {user_id}: {e}")
            failure_count += 1

    logger.info(f"✅ Restart notification sent to {success_count} users. Failed for {failure_count} users.")


# --- Main Asynchronous Bot Runner ---
async def run_bot_async(app: Client):
    """Starts the client, runs the notification task, and keeps the client alive."""
    
    logger.info("Starting bot client connection...")
    
    # 1. Start the client to establish the connection and make it ready for sending messages.
    await app.start()
    SERIES_CACHE[:] = await search_series("")
    SERIES_CACHE_LC[:] = [x.lower() for x in SERIES_CACHE]
    logger.info(f"SERIES CACHE LOADED: {len(SERIES_CACHE)} entries")
    # 2. Schedule the notification task to run concurrently after the client is connected.
    asyncio.create_task(notify_users_of_restart(app))
    
    logger.info("Bot is running. Waiting for updates...")
    
    # 3. Keep the script alive and listening for updates.
    # REPLACED: await app.idle() with an infinite await
    try:
        # Await a Future that will never be done, keeping the loop running indefinitely
        await asyncio.Future() 
    except KeyboardInterrupt:
        # Allows cleanup on Ctrl+C or process termination
        pass
    
    # 4. Cleanly stop the client on exit.
    await app.stop()


def main():
    """Initializes and runs the Pyrogram client, including SQL setup."""
    
    # --- SQL DB Initialization and Migration ---
    logger.info("Initializing and migrating data to SQL database...")
    try:
        initialize_sql_db()
        logger.info("✅ SQL database is ready.")
    except Exception as e:
        logger.error(f"❌ Critical failure during SQL DB initialization/migration: {e}")
        return
        
    # --- Pyrogram Client Setup ---
    app = Client(
        "SeriesBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    # Register handlers
    register_command_handlers(app)
    register_conversation_handlers(app)
    
    # --- Start the bot ---
    logger.info("Starting bot client main loop...")
    # This is the blocking call that executes the async function run_bot_async
    app.run(run_bot_async(app))

if __name__ == "__main__":
    main()
