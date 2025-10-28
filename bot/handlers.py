import logging
from telegram import Update
from telegram.ext import ContextTypes

# Get the logger for this module
logger = logging.getLogger(__name__)

# --- Command Handlers ---
# In bot/handlers.py
from bot.database import add_series_entry # Import the new function

# ... (Existing command handlers) ...

async def addfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE, db_collection, admin_id: int) -> None:
    """
    Admin command to add a new file (series episode) to the database.
    Usage: /addfile Title SXXEYY File_ID
    """
    user_id = update.message.from_user.id
    
    # 1. Security Check: Only allow the admin
    if user_id != admin_id:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return
        
    # 2. Check for required arguments
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/addfile Series Title, Season/Episode (e.g. S01E01), Telegram_File_ID`\n"
            "Example: `/addfile The Witcher S03E01 BAgADLgADaYj`",
            parse_mode='Markdown'
        )
        return

    # 3. Parse arguments (Simplified parsing for demonstration)
    
    # We assume the user sends: /addfile "Title with spaces" SXXEYY FileID
    # A more robust parser is needed for production. Here, we take the whole text
    # after /addfile and split it.
    full_text = update.message.text.split(' ', 1)[1] # Get everything after /addfile
    
    # Find the SXXEYY pattern and the file ID at the end
    import re
    
    # Pattern to find SXXEYY (where XX and YY are digits)
    season_episode_match = re.search(r'(s\d+e\d+)', full_text, re.IGNORECASE)
    
    # The file ID is usually a long string at the end
    file_id_match = full_text.split()[-1]
    
    if not season_episode_match or not file_id_match:
        await update.message.reply_text("Could not parse the Season/Episode or File ID. Please check the format.")
        return

    # Extract SXX, EYY numbers and series title
    se_code = season_episode_match.group(1).upper() # e.g., 'S03E01'
    season = int(se_code[1:3])
    episode = int(se_code[4:6])
    
    # The title is everything before the SE code
    title_raw = full_text[:season_episode_match.start()].strip()

    # 4. Prepare the data dictionary
    series_data = {
        "title": title_raw,
        "season": season,
        "episode": episode,
        "file_id": file_id_match, # The unique ID Telegram gives a file
        "link": f"tg://openmessage?user_id={context.bot.id}&message_id={update.message.message_id}", # Or link to your channel post
        "timestamp": update.message.date
    }

    # 5. Insert the data and provide feedback
    inserted_id = add_series_entry(db_collection, series_data)

    if inserted_id:
        await update.message.reply_text(
            f"✅ **File Added!**\n\n"
            f"Title: `{title_raw} {se_code}`\n"
            f"DB ID: `{inserted_id}`",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Failed to add file to database.")
        
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a greeting message when the /start command is issued."""
    await update.message.reply_text(
        '👋 Welcome! Send me the name of the series, season, or episode you are looking for.\n'
        'Example: *Game of Thrones S08E06*',
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a help message when the /help command is issued."""
    await update.message.reply_text(
        'I am a Series Filter Bot. I search my database for your requested series content.\n\n'
        '**Features to add:**\n'
        '* Search by Series Name\n'
        '* Filter by Season and Episode\n'
        '* Advanced-text matching (Regex/Fuzzy Search in MongoDB)'
    )

# --- Message Handler (The Core Filtering Logic) ---

async def filter_series(update: Update, context: ContextTypes.DEFAULT_TYPE, db_collection) -> None:
    """
    Handles user text input and attempts to find matching series in MongoDB.
    
    NOTE: This is a simplified search. A real filter bot would use advanced 
    text parsing (like regex or pattern matching) to extract Season/Episode numbers.
    """
    user_query = update.message.text.strip()
    
    # 1. Basic Sanitization (and converting to a case-insensitive query for MongoDB)
    search_term = user_query.lower()
    
    # 2. Query MongoDB
    # This queries based on a partial, case-insensitive match on the 'title' field
    mongo_query = {"title": {"$regex": search_term, "$options": "i"}}
    results = list(db_collection.find(mongo_query).limit(5)) # Limit results for cleaner output
    
    # 3. Respond to the User
    if results:
        response = f"✨ **Found {len(results)} potential matches for '{user_query}':**\n\n"
        for i, doc in enumerate(results):
            # Safe retrieval for display
            title = doc.get('title', 'N/A')
            season = doc.get('season', '?')
            episode = doc.get('episode', '?')
            link = doc.get('link', 'No Link Available')
            
            response += (
                f"**{i+1}.** **{title}** (S{season:02d}E{episode:02d})\n"
                f"🔗 [Get File]({link})\n"
            )
        
        await update.message.reply_text(response, parse_mode='Markdown')
        
    else:
        await update.message.reply_text(
            f"❌ Sorry, I could not find any series matching *'{user_query}'* in my database.",
            parse_mode='Markdown'
        )

# --- Error Handler ---

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a message to the user."""
    logger.error('Update "%s" caused error "%s"', update, context.error)
    if update and update.effective_chat:
        await update.effective_chat.send_message(
            text=f'An error occurred: {context.error}',
            parse_mode='Markdown'
        )
