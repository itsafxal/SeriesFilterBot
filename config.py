# config.py

import logging
from pymongo import TEXT 

# --- Bot Configuration ---
API_ID = 28643132
API_HASH = "510c3009ea589830a89e045bf8aae656"
BOT_TOKEN = "7661197159:AAHgvfHL2qK-r-7uDiNkAjROGL1nYjM6cZE"
MONGO_URI = "mongodb+srv://ss:ss@seriesfilterbot.p88od4y.mongodb.net/?appName=SeriesFilterBOT"

# --- Indexing Configuration ---
INDEX_CHANNELS = [-1001996538290] 
ADMIN_USERS = [5483128891] # ⚠️ REPLACE ME: Your Telegram User ID(s)
FORCE_SUB_CHANNEL = None   

#Image/Poster Configuration
START_IMG = "https://sanishtech.com/i/690cfef2312bd-1762459378.jpg"
DEFAULT_POSTER_URL = "https://sanishtech.com/i/690ceab460363-1762454196.jpg"
OMDB_API_KEY = ""

#captions
CUSTOM_CAPTION_TEXT = """
`{file_name}`

🔗 **Please Use Our Bot **
@{bot_username}
"""

ADMIN_TEXT ="""
HLO TEST
"""
ABOUT_TEXT = """ <blockquote>
‣ ᴍʏ ɴᴀᴍᴇ : <a href='https://t.me/TGxSeriesBot'>Sᴇʀɪᴇs Bᴏᴛ</a>
‣ ᴄʀᴇᴀᴛᴏʀ : <a href='tg://settings'>ᴛʜɪs ᴘᴇʀsᴏɴ</a>
‣ ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href='https://t.me/CyberTGx'>ᴄʏʙᴇʀ ᠰ ᴛɢ</a>
‣ ʟɪʙʀᴀʀʏ : <a href='https://docs.pyrogram.org/'>ᴘʏʀᴏɢʀᴀᴍ</a>
‣ ʟᴀɴɢᴜᴀɢᴇ : <a href='https://www.python.org/download/releases/3.0/'>ᴘʏᴛʜᴏɴ 3</a>
‣ ᴘʀɪᴍᴀʀʏ ᴅʙ : <a href='https://www.mongodb.com/'>ᴍᴏɴɢᴏ ᴅʙ</a>
‣ sᴇᴄᴏɴᴅᴀʀʏ ᴅʙ : <a href='https://www.freesqldatabase.com/'>sᴏ̨ʟ ᴅʙ</a>
‣ ʙᴏᴛ sᴇʀᴠᴇʀ : <a href='https://t.me/quickfastt'>ǫᴜɪᴄᴋ ꜰᴀsᴛ</a></b></blockquote>"""

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("config")
