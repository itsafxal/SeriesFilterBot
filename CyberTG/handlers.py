# handlers.py
import asyncio
import re
import shelve
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from pyrogram.errors import MessageNotModified
from DB.database import (
    search_series,
    get_all_files_by_season_and_quality,
    get_series_summary,
    get_global_setting,
    get_file_by_message_id_and_channel_id,
)
from config import (
    logger,
    CUSTOM_CAPTION_TEXT,
    DEFAULT_POSTER,
    ADMIN_USERS,
    OMDB_CACHE_DB,
)
from utils import (
    ABOUT_KEYBOARD,
    ADMIN_TEXT,
    ABOUT_TEXT,
    clean_series_name,
    delete_message_later,
    check_force_subscribe,
)
from CyberTG.poster import get_series_poster_async
from menus import show_language_menu, show_quality_menu # Import from the new menus file

MAX_CALLBACK_LENGTH = 50


def register_conversation_handlers(cyber: Client):
    
    @cyber.on_message(filters.text & filters.private & ~filters.command(["start", "imdb", "autodel", "forcesub"]))
    async def search_handler(cyber, message: Message):
        """Handles text messages for series search."""
        query = message.text.strip()
        if query.startswith("/") and query.lower() not in ["/index"]:
            return

        # Force Subscribe Check
        is_member, invite_link = await check_force_subscribe(cyber, message.from_user.id)
        if not is_member:
            await message.reply_text(
                "🛑 **Please join our channel to get search results.**\n\n"
                "Click the button below to join:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 Join Channel", url=invite_link)]]),
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            return

        # --- Primary Search ---
        try:
            unique_names = await search_series(query)
        except Exception as e:
            logger.warning(f"[SEARCH ERROR] search_series failed for ORIGINAL query `{query}`: {e}")
            unique_names = []

        if not unique_names:
            # --- Cleaned Search/Suggestion Logic ---
            clean_query = re.sub(
                r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\sme)?)|movie(s)?|new|latest|br((o|u)h?)*|^h(e|a)?(l)*(o)*|mal(ayalam)?|t(h)?amil|file|that|find|und(o)*|kit(t(i|y)?)?o(w)?|thar(u)?(o)*w?|kittum(o)*|aya(k)*(um(o)*)?|full\smovie|any(one)|with\ssubtitle(s)?)",
                "", message.text, flags=re.IGNORECASE).strip()
            
            suggestion_list = []
            
            if clean_query:
                try:
                    suggestion_list = await search_series(clean_query)
                except Exception as e:
                    logger.warning(f"[SEARCH ERROR] search_series failed for CLEANED query `{clean_query}`: {e}")

            if suggestion_list:
                movielist = [clean_series_name(name) for name in suggestion_list]
                movielist = list(dict.fromkeys(movielist))
                
                if len(movielist) > 3:
                    movielist = movielist[:3]
                user_id = message.from_user.id
                
                btn = [[
                    InlineKeyboardButton(
                        text=movie,
                        callback_data=f"sugg_select|{user_id}|{movie}",
                    )
                ] for movie in movielist]
                
                btn.append([InlineKeyboardButton(text="Close", callback_data=f'close_suggestion|{user_id}')])
                
                sent_message = await message.reply(
                    f"I couldn't find anything related to `<code>{query}</code>`.\nDid you mean any one of these?",
                    reply_markup=InlineKeyboardMarkup(btn),
                    parse_mode=enums.ParseMode.HTML
                )
                
                auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
                if auto_delete_timer and int(auto_delete_timer) > 0:
                    asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, int(auto_delete_timer)))
                
                return

            else:
                await message.reply_text(f"❌ No series found `<code>{query}</code>`", parse_mode=enums.ParseMode.HTML)
                return
        
        # --- Display Search Result ---
        series_to_display = clean_series_name(unique_names[0])

        poster_url = DEFAULT_POSTER
        imdb_enabled = True
        try:
            imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
        except Exception:
            pass

        # Check Cache for Poster
        try:
            cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
            with shelve.open(cache_file) as db_cache:
                key = f"poster::{series_to_display.strip().lower()}"
                if key in db_cache:
                    poster_url = db_cache[key]
        except Exception:
            pass

        # Fetch Poster if needed
        if (not poster_url or poster_url == DEFAULT_POSTER) and imdb_enabled:
            try:
                poster_tmp = await get_series_poster_async(series_to_display)
                poster_url = poster_tmp or DEFAULT_POSTER
            except Exception as e:
                logger.warning(f"[OMDB FETCH FAIL - search_handler] {series_to_display} -> {e}")
                poster_url = DEFAULT_POSTER

        summary = await get_series_summary(series_to_display)

        languages_list = ", ".join(summary.get("languages", []))
        seasons_count = len(summary.get("seasons", []))
        qualities_list = ", ".join(summary.get("qualities", []))

        response = (
            f"**○ Title:** {series_to_display}\n"
            f"**○ Language:** {languages_list or 'N/A'}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )

        keyboard = []
        series_name_short = series_to_display[:MAX_CALLBACK_LENGTH]
        for season in summary.get("seasons", []):
            callback_data = f"ss|{series_name_short}|{season}"
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        try:
            sent_message = await message.reply_photo(
                photo=poster_url,
                caption=response,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Failed to reply with photo for search result: {e}. Falling back to reply_text.")
            sent_message = await message.reply_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)

        if auto_delete_timer and int(auto_delete_timer) > 0:
            asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, int(auto_delete_timer)))


    @cyber.on_callback_query(filters.regex(r"^sugg_select\|"))
    async def sugg_select_handler(cyber, query):
        """Handles selection from search suggestions."""
        data = query.data.split("|", 2)
        if len(data) < 3:
            await query.answer("❌ Error: Invalid suggestion data.", show_alert=True)
            return

        series_name = data[2]
        await query.answer(f"Selected: {series_name}...", cache_time=0)

        poster_url = DEFAULT_POSTER
        imdb_enabled = True
        try:
            imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
        except Exception:
            pass

        # Check Cache for Poster
        try:
            cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
            with shelve.open(cache_file) as db:
                key = f"poster::{series_name.strip().lower()}"
                if key in db:
                    poster_url = db[key]
        except Exception:
            pass

        # Fetch Poster if needed
        if (not poster_url or poster_url == DEFAULT_POSTER) and imdb_enabled:
            try:
                poster_tmp = await get_series_poster_async(series_name)
                poster_url = poster_tmp or DEFAULT_POSTER
            except Exception as e:
                logger.warning(f"[OMDB FETCH FAIL - sugg_select] {series_name} -> {e}")
                poster_url = DEFAULT_POSTER

        summary = await get_series_summary(series_name)
        languages_list = ", ".join(summary.get("languages", []))
        seasons_count = len(summary.get("seasons", []))
        qualities_list = ", ".join(summary.get("qualities", []))

        response = (
            f"**○ Title:** {series_name}\n"
            f"**○ Language:** {languages_list or 'N/A'}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )
        
        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary.get("seasons", []):
            callback_data = f"ss|{series_name_short}|{season}"
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        try:
            # Edit the suggestion message to display the series info
            await query.message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                reply_markup=reply_markup,
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Failed to edit media for sugg_select: {e}. Falling back to edit_text.")
            try:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass

        if auto_delete_timer and int(auto_delete_timer) > 0:
            asyncio.create_task(delete_message_later(cyber, query.message.chat.id, query.message.id, int(auto_delete_timer)))


    @cyber.on_callback_query(filters.regex(r"^close_suggestion\|"))
    async def close_suggestion_handler(cyber, query):
        """Handles closing the suggestion message."""
        try:
            await cyber.delete_messages(query.message.chat.id, query.message.id)
        except Exception as e:
            logger.warning(f"Failed to delete suggestion message: {e}")
            await query.answer("❌ Failed to close message.", show_alert=True)
            return
        await query.answer("Closed.", show_alert=False)


    @cyber.on_callback_query(filters.regex(r"^ss\|"))
    async def season_select_handler(cyber, query):
        """Handles season selection and shows the language menu."""
        data = query.data.split("|")

        if len(data) < 3:
            logger.error(f"Malformed callback data for season_select_handler: {query.data}")
            await query.answer("❌ Error: Invalid season selection data. Please try searching again.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            logger.error(f"Callback data had non-integer season: {data[2]}")
            await query.answer("❌ Error: Invalid season number format.", show_alert=True)
            return

        # Use the function from menus.py
        await show_language_menu(cyber, query, series_name, season)
        await query.answer()


    @cyber.on_callback_query(filters.regex(r"^ls\|"))
    async def language_select_handler(cyber, query):
        """Handles language selection and shows the quality menu."""
        data = query.data.split("|")
        if len(data) < 4:
            await query.answer("❌ Error: Invalid language selection data.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number.", show_alert=True)
            return
        language = data[3]
        
        # Use the function from menus.py
        await show_quality_menu(cyber, query, series_name, language, season)
        await query.answer()


    @cyber.on_callback_query(filters.regex(r"^bl\|"))
    async def back_to_language_handler(cyber, query):
        """Handles going back to the language selection menu."""
        data = query.data.split("|")

        if len(data) < 3:
            await query.answer("❌ Error: Invalid back data.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number format.", show_alert=True)
            return

        # Use the function from menus.py
        await show_language_menu(cyber, query, series_name, season)
        await query.answer()

    
    @cyber.on_callback_query(filters.regex(r"^qs\|"))
    async def quality_select_handler(cyber, query):
        """Handles quality selection and sends all files for the season/quality/language."""
        data = query.data.split("|")
        if len(data) < 5:
            await query.answer("❌ Error: Invalid quality selection data.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number.", show_alert=True)
            return

        language = data[3]
        quality = data[4]

        await query.answer(f"Sending {quality}/{language} episodes for S{season}...", cache_time=0)

        files = await get_all_files_by_season_and_quality(series_name, season, quality, language)
        if not files:
            await query.answer("❌ No files found.", show_alert=True)
            return

        bot_username = (await cyber.get_me()).username
        sent_count = 0

        for file_entry in files:
            channel_id = file_entry["channel_id"]
            message_id = file_entry["message_id"]
            file_name = file_entry.get("file_name", f"S{file_entry.get('season', 0)}E{file_entry.get('episode', 99)}")

            custom_caption = CUSTOM_CAPTION_TEXT.format(file_name=file_name, bot_username=bot_username)

            try:
                file_id = file_entry.get("file_id")
                if file_id:
                    # Send as document using file_id
                    await cyber.send_document(
                        chat_id=query.from_user.id,
                        document=file_id,
                        caption=custom_caption,
                        parse_mode=enums.ParseMode.MARKDOWN,
                    )
                else:
                    # Fallback to copy_message using channel_id and message_id
                    await cyber.copy_message(
                        chat_id=query.from_user.id,
                        from_chat_id=channel_id,
                        message_id=message_id,
                        caption=custom_caption,
                        parse_mode=enums.ParseMode.MARKDOWN,
                    )
                sent_count += 1
            except Exception as e:
                logger.error(f"Copy/Send fail for {message_id} in {channel_id}: {e}")
                continue
                
            if sent_count < len(files):
                await asyncio.sleep(1.5) # Throttle sending

        success_text = (
            f"✅ Successfully sent {sent_count} files from "
            f"**{series_name}** — Season {season} ({quality})"
        )
        await cyber.send_message(chat_id=query.from_user.id, text=success_text, parse_mode=enums.ParseMode.MARKDOWN)


    @cyber.on_callback_query(filters.regex(r"^fs\|"))
    async def file_select_handler(cyber, query):
        """Handles single file selection."""
        data = query.data.split("|")
        if len(data) < 4:
            await query.answer("❌ Error: Invalid file selection data.", show_alert=True)
            return

        try:
            channel_id = int(data[2])
            message_id = int(data[3])
        except ValueError:
             await query.answer("❌ Error: Invalid Channel/Message ID.", show_alert=True)
             return
             
        await query.answer("Sending episode file...", cache_time=0)

        file_entry = await get_file_by_message_id_and_channel_id(channel_id, message_id)
        if not file_entry:
            await query.answer("❌ File link not found.", show_alert=True)
            return

        file_name = file_entry.get("file_name", f"S{file_entry.get('season',0)}E{file_entry.get('episode',99)}")

        try:
            bot_username = (await cyber.get_me()).username
            custom_caption = CUSTOM_CAPTION_TEXT.format(file_name=file_name, bot_username=bot_username)

            await cyber.copy_message(
                chat_id=query.from_user.id,
                from_chat_id=channel_id,
                message_id=message_id,
                caption=custom_caption,
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            await query.answer(f"✅ Sent '{file_name}'!", show_alert=True)
        except Exception as e:
            logger.error(f"Failed to copy file {message_id} from {channel_id}: {e}")
            await query.answer("❌ Failed to send file.\nMake sure bot is admin.", show_alert=True)

    
    @cyber.on_callback_query(filters.regex(r"^back_to_search\|"))
    async def back_to_search_handler(cyber, query):
        """Handles going back to the main search result (season selection) menu."""
        data = query.data.split("|")
        if len(data) < 2:
            await query.answer("❌ Error: Invalid back data.", show_alert=True)
            return

        series_name = data[1]

        poster_url = DEFAULT_POSTER
        imdb_enabled = True
        try:
            imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
        except Exception:
            pass

        # Check Cache for Poster
        try:
            cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
            with shelve.open(cache_file) as db:
                key = f"poster::{series_name.strip().lower()}"
                if key in db:
                    poster_url = db[key]
        except Exception:
            pass

        # Fetch Poster if needed
        if (not poster_url or poster_url == DEFAULT_POSTER) and imdb_enabled:
            try:
                poster_tmp = await get_series_poster_async(series_name)
                poster_url = poster_tmp or DEFAULT_POSTER
            except Exception as e:
                logger.warning(f"[OMDB FETCH FAIL - back_to_search] {series_name} -> {e}")
                poster_url = DEFAULT_POSTER

        summary = await get_series_summary(series_name)
        languages_list = ", ".join(summary.get("languages", []))
        seasons_count = len(summary.get("seasons", []))
        qualities_list = ", ".join(summary.get("qualities", []))

        response = (
            f"**○ Title:** {series_name}\n"
            f"**○ Language:** {languages_list or 'N/A'}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary.get("seasons", []):
            callback_data = f"ss|{series_name_short}|{season}"
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)

        try:
            await query.message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                reply_markup=reply_markup,
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Failed to edit media for back_to_search: {e}. Falling back to edit_text.")
            try:
                await query.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass

        if auto_delete_timer and int(auto_delete_timer) > 0:
            asyncio.create_task(delete_message_later(cyber, query.message.chat.id, query.message.id, int(auto_delete_timer)))

        await query.answer()

    
    @cyber.on_callback_query(filters.regex(r"^help\|"))
    async def help_menu_handler(cyber, query):
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

    
    @cyber.on_callback_query(filters.regex(r"^cyber\|"))
    async def cyber_menu_handler(cyber, query):
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
            await query.message.edit_caption(
                START_TEXT,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in cyber_menu_handler: {e}")
            try:
                await query.message.edit_text(
                    START_TEXT,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass

        await query.answer("Back To Home")

    
    @cyber.on_callback_query(filters.regex(r"^admin_cmd\|"))
    async def admin_menu_handler(cyber, query):
        """Displays the admin menu (restricted)."""
        user_id = query.from_user.id
        is_authorized = False
        try:
            if isinstance(ADMIN_USERS, (list, tuple, set)):
                is_authorized = user_id in ADMIN_USERS
            else:
                is_authorized = (user_id == int(ADMIN_USERS))
        except Exception:
            is_authorized = False

        if not is_authorized:
            await query.answer("You are not authorized. Only the admin can open this menu.", show_alert=True)
            return

        about_text = ADMIN_TEXT
        ADMIN_KEYBOARD = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏡 ʜᴏᴍᴇ", callback_data="cyber|"),
             InlineKeyboardButton("🚫 ᴄʟᴏsᴇ", callback_data="close")]
        ])
        reply_markup = ADMIN_KEYBOARD
        try:
            await query.message.edit_caption(
                about_text,
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

        await query.answer("Showing about information.")

    
    @cyber.on_callback_query(filters.regex(r"^about\|"))
    async def about_menu_handler(cyber, query):
        """Displays the about menu."""
        about_text = ABOUT_TEXT
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

    
    @cyber.on_callback_query(filters.regex(r"close"))
    async def close_menu_handler(_, query):
        """Deletes the message."""
        try:
            await query.message.delete()
            await query.answer("Closed.")
        except Exception:
            await query.answer("Can't close.")

    return [
        search_handler, season_select_handler, language_select_handler,
        back_to_language_handler, quality_select_handler, file_select_handler,
        back_to_search_handler, help_menu_handler, about_menu_handler,
        admin_menu_handler, cyber_menu_handler, close_menu_handler,
        close_suggestion_handler, sugg_select_handler
    ]
