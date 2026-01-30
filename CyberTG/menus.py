import asyncio
import shelve
from pyrogram import Client, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, CallbackQuery
from pyrogram.errors import MessageNotModified


async def help_menu_handler(cyber: Client, query: CallbackQuery):
    """Displays the help menu."""
    help_text = (
        "<b>💡 Help & Instructions</b>\n\n"
        "1. <b>Search</b>: Simply send the <b>name</b> of the movie or series you are looking for.\n"
        "2. <b>Select</b>: Tap the <b>Season</b> button for a series.\n"
        "3. <b>Filter</b>: Choose the <b>Language</b> and then the <b>Quality</b>.\n"
        "4. <b>Receive</b>: The bot will automatically start sending the files to you.\n\n"
        "<i>If you see 'No series found', try a slightly different or shorter name.</i>"
    )

    HELP_KEYBOARD = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏡 ʜᴏᴍᴇ", callback_data="cyber|"),
         InlineKeyboardButton("🔅 ᴀᴅᴍɪɴ (ᴏɴʟʏ)", callback_data="admin_cmd|")]
    ])
    reply_markup = HELP_KEYBOARD

    try:
        await query.message.edit_caption(
            help_text,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in help_menu_handler: {e}")
        try:
            await query.message.edit_text(
                help_text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass

    await query.answer("Showing help menu.")


async def cyber_menu_handler(cyber: Client, query: CallbackQuery):
    """Displays the main bot/home menu."""
    user = query.from_user
    bot = await cyber.get_me()
    user_mention = user.mention
    START_TEXT = (
        f"🖤 <b>Hey {user_mention}!</b>\n\n"
        f"Welcome to <b>{bot.first_name}</b> — your private cinema in chat 🎭\n"
        "Just type what you’re looking for and I’ll fetch it faster than light ⚡\n\n"
        "<blockquote>Let’s dive into the world of entertainment 🌙</blockquote>"
    )
    CYBER_KEYBOARD = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎞 ʙʀᴏᴡsᴇ ʟɪʙʀᴀʀʏ 🎞", switch_inline_query_current_chat="")
        ],[
            InlineKeyboardButton("⚡ ʜᴇʟᴘ", callback_data="help|menu"),
            InlineKeyboardButton("ᴀʙᴏᴜᴛ ✨", callback_data="about|menu")
        ],[
            InlineKeyboardButton("🍷 sᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ 🍷", url="t.me/TomxSupport")
        ]
    ])
    reply_markup = CYBER_KEYBOARD
    try:
        await query.message.edit_media(
            media=InputMediaPhoto(media=DEFAULT_POSTER, caption=START_TEXT, parse_mode=enums.ParseMode.HTML),
            reply_markup=reply_markup,
        )
    except MessageNotModified:
        pass
    except Exception as e:
        # Fallback to edit_text if it fails to edit the media (e.g., if the original message was text)
        logger.error(f"Error editing message media/caption in cyber_menu_handler: {e}")
        try:
            await query.message.edit_text(
                START_TEXT,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass

    await query.answer("Back To Home")


async def admin_menu_handler(cyber: Client, query: CallbackQuery):
    """Displays the admin menu (restricted)."""
    user_id = query.from_user.id
    is_authorized = False
    try:
        # Check if user_id is in ADMIN_USERS (handling both list and single ID configurations)
        if isinstance(ADMIN_USERS, (list, tuple, set)):
            is_authorized = user_id in ADMIN_USERS
        else:
            is_authorized = (user_id == int(ADMIN_USERS))
    except Exception:
        is_authorized = False

    if not is_authorized:
        await query.answer("You are not authorized. Only the admin can open this menu.", show_alert=True)
        return

    admin_text = (
        f"**𝒯𝐻𝐼𝒮 𝒴𝒪𝒰𝑅 𝒜𝒟𝑀𝐼𝒩 𝒞𝒪𝑀𝑀𝒜𝒩𝒟𝒮**\n\n"
        f"/setskip - To Skip Indexing File range\n"
        f"/broadcast - To broadcast Message To users\n"
        f"/deleteall - Delete All Files From Database\n"
        f"/deletefile  - Delete a replyed File from database\n"
        f"/total - Get Total Uses and Files\n"
        f"/imdb - To Turn on and off Imdb\n"
        f"/fsub - To Set Force Subscribe\n"
        f"/autodel - To Set Autodelete Time\n"
        f"/addindex - To Add Index Channel\n"
        f"/delindex - Delete Index Channel\n"
        f"/listindex - To List All Indexed Channels\n"
    )
    ADMIN_KEYBOARD = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏡 ʜᴏᴍᴇ", callback_data="cyber|"),
         InlineKeyboardButton("🚫 ᴄʟᴏsᴇ", callback_data="close")]
    ])
    reply_markup = ADMIN_KEYBOARD
    try:
        await query.message.edit_caption(
            admin_text,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in admin_menu_handler: {e}")
        try:
            await query.message.edit_text(
                about_text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass

    await query.answer("Showing admin information.")


async def about_menu_handler(cyber: Client, query: CallbackQuery):
    """Displays the about menu."""
    about_text = (
        f"<blockquote>‣ ᴍʏ ɴᴀᴍᴇ : <a href='https://t.me/TGxSeriesBot'>Sᴇʀɪᴇs Bᴏᴛ</a>\n"
        f"‣ ᴄʀᴇᴀᴛᴏʀ : <a href='tg://settings'>ᴛʜɪs ᴘᴇʀsᴏɴ</a>\n"
        f"‣ ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href='https://t.me/CyberTGx'>ᴄʏʙᴇʀ ᠰ ᴛɢ</a>\n"
        f"‣ ʟɪʙʀᴀʀʏ : <a href='https://docs.pyrogram.org/'>ᴘʏʀᴏɢʀᴀᴍ</a>\n"
        f"‣ ʟᴀɴɢᴜᴀɢᴇ : <a href='https://www.python.org/download/releases/3.0/'>ᴘʏᴛʜᴏɴ 3</a>\n"
        f"‣ ᴘʀɪᴍᴀʀʏ ᴅʙ : <a href='https://www.mongodb.com/'>ᴍᴏɴɢᴏ ᴅʙ</a>\n"
        f"‣ sᴇᴄᴏɴᴅᴀʀʏ ᴅʙ : <a href='https://www.freesqldatabase.com/'>sᴏ̨ʟ ᴅʙ</a>\n"
        f"‣ ʙᴏᴛ sᴇʀᴠᴇʀ : <a href='https://t.me/quickfastt'>ǫᴜɪᴄᴋ ꜰᴀsᴛ</a></b></blockquote>"
    )
    ABOUT_KEYBOARD = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("💻 Source Code", url="https://github.com/CyberTG/SeriesFilterBot")
    ],[
        InlineKeyboardButton("🏡 ʜᴏᴍᴇ", callback_data="cyber|home"),
        InlineKeyboardButton("🕵‍♂ ʜᴇʟᴘ", callback_data="help|menu")
    ]])
    reply_markup = ABOUT_KEYBOARD
    try:
        await query.message.edit_caption(
            about_text,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editing message caption in about_menu_handler: {e}")
        try:
            await query.message.edit_text(
                about_text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass

    await query.answer("Showing about information.")


async def close_menu_handler(_, query: CallbackQuery):
    """Deletes the message."""
    try:
        await query.message.delete()
        await query.answer("Closed.")
    except Exception:
        await query.answer("Can't close.")
