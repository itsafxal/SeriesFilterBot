# WARNING: This script uses the Telethon client library, NOT the python-telegram-bot library.
# Install Telethon: pip install Telethon

import asyncio
from telethon import TelegramClient
from telethon.tl.types import Message
from telethon.errors.rpcerrorlist import ChannelPrivateError
from datetime import datetime

# --- Bot's Local Imports ---
# Ensure database.py and utils.py are in the same directory
from database import init_db, add_series_files_bulk, logger
from utils import parse_series_info 
from config import API_ID, API_HASH, TARGET_CHANNEL, SESSION_NAME


async def index_channel_history():
    """Iterates through channel messages, extracts series info, and bulk inserts."""
    
    # 1. Initialize DB and Telethon Client
    logger.info("Initializing bot database connection...")
    init_db()
    
    logger.info("Starting Telegram client...")
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()

    # 2. Get Channel Entity
    try:
        channel_entity = await client.get_entity(TARGET_CHANNEL)
    except ChannelPrivateError:
        logger.error(f"Failed to access channel: {TARGET_CHANNEL}. Ensure the account is a member and the username/ID is correct.")
        await client.disconnect()
        return
    except Exception as e:
        logger.error(f"Error getting channel entity: {e}")
        await client.disconnect()
        return

    logger.info(f"Successfully connected to channel: {channel_entity.title}")

    # Determine channel_id format for the database
    # Telethon uses raw ID; our bot uses -100 + ID for private/supergroups.
    if channel_entity.id < 0:
        db_channel_id = str(channel_entity.id)
    else:
        db_channel_id = str(-100 * 1000000000 + channel_entity.id)


    # 3. Iterate and Collect Data
    documents_to_insert = []
    messages_processed = 0
    batch_size = 500 # Insert to DB in batches

    async for message in client.iter_messages(channel_entity, reverse=True):
        if not isinstance(message, Message):
            continue

        messages_processed += 1
        
        # We only care about messages that actually have a file and text/caption to parse
        if message.media and message.file and message.text:
            
            # Use the existing series parsing logic
            series_info = parse_series_info(message.text)
            
            if series_info:
                series_name, season, episode = series_info
                
                documents_to_insert.append({
                    "series_name": series_name,
                    "season": season,
                    "episode": episode,
                    "channel_id": db_channel_id,
                    "message_id": message.id,
                    "indexed_at": datetime.utcnow()
                })

        # 4. Perform Bulk Insert when batch size is reached
        if len(documents_to_insert) >= batch_size:
            successful, duplicates = add_series_files_bulk(documents_to_insert)
            logger.info(f"Processed {messages_processed} messages. Batch inserted: {successful} new, {duplicates} duplicates.")
            documents_to_insert = [] # Clear the batch

    # 5. Insert any remaining documents
    if documents_to_insert:
        successful, duplicates = add_series_files_bulk(documents_to_insert)
        logger.info(f"Final batch inserted: {successful} new, {duplicates} duplicates.")

    logger.info(f"--- Channel Indexing Complete ---")
    logger.info(f"Total messages processed: {messages_processed}")
    
    await client.disconnect()

if __name__ == '__main__':
    # You need to run this file like a regular Python script: 
    # python index_channel_history.py
    # The first time, Telethon will ask you for your phone number and login code.
    try:
        asyncio.run(index_channel_history())
    except KeyboardInterrupt:
        logger.warning("Indexing stopped by user.")
