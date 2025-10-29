# create_session.py

from pyrogram import Client
from config import API_ID, API_HASH, INDEXER_SESSION_NAME

# This script creates the session file required for the indexing user client.

async def main():
    """Starts the user client to perform the interactive login."""
    # Use the session name defined in config.py
    user_app = Client(
        name=INDEXER_SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=False  # Must be False to save the session file
    )
    
    print(f"Starting interactive login for the '{INDEXER_SESSION_NAME}' user session...")
    print("You will be prompted for your phone number, code, and 2FA password (if set).")
    
    # Start the client, which handles the interactive login
    await user_app.start()
    
    # Stop immediately after successful login
    await user_app.stop()
    
    print("\n✅ Success! The session file 'IndexerUser.session' has been created.")
    print("You can now run the main bot script.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
