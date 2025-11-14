# groupfilter.py
import asyncio
import re
import shelve
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    CallbackQuery,
)
from pyrogram.errors import MessageNotModified

# Assuming these are available from your project structure
from DB.database import (
    search_series,
    get_series_summary,
    get_global_setting,
    get_unique_languages_by_season,
    get_unique_qualities_by_lang_season,
)
from config import (
    logger,
    DEFAULT_POSTER,
    OMDB_CACHE_DB,
    MAX_CALLBACK_LENGTH,
)
from utils import (
    clean_series_name,
    delete_message_later,
)
from .poster import get_series_poster_async


# --- Group Search Handler ---

def register_group_handlers(cyber: Client):
    
    @cyber.on_message(filters.text & filters.group & ~filters.command(["start", "imdb"]))
    async def group_search_handler(cyber, message: Message):
        """
        Handles text messages in groups for series search.
        Displays suggestions or the main season menu for the best match.
        """
        query = message.text.strip()
        if query.startswith("/") and query.lower() not in ["/index"]:
            return

        # Basic query cleaning
        clean_query = re.sub(
            r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\sme)?)|movie(s)?|new|latest|br((o|u)h?)*|^h(e|a)?(l)*(o)*|mal(ayalam)?|t(h)?amil|file|that|find|und(o)*|kit(t(i|y)?)?o(w)?|thar(u)?(o)*w?|kittum(o)*|aya(k)*(um(o)*)?|full\smovie|any(one)|with\ssubtitle(s)?)",
            "", message.text, flags=re.IGNORECASE).strip()

        search_term = clean_query or query
        if not search_term:
            return

        try:
            # Search for series names
            all_found_names = await search_series(search_term) 
        except Exception as e:
            logger.warning(f"[GROUP SEARCH ERROR] search_series failed for `{search_term}`: {e}")
            all_found_names = []
            
        unique_names = list(dict.fromkeys([clean_series_name(name) for name in all_found_names]))

        if not unique_names:
            # Do not spam the group chat with "No series found"
            return 

        # --- Display Suggestions or Single Result ---
        
        if len(unique_names) > 1:
            # If multiple results (suggestions) were found, display the top 5
            movielist = unique_names[:5] 
            user_id = message.from_user.id
            
            btn = [[
                InlineKeyboardButton(
                    text=movie,
                    # Use 'gsugg_select' for group suggestion selection
                    callback_data=f"gsugg_select|{user_id}|{movie}", 
                )
            ] for movie in movielist]
            
            # Use 'close_suggestion' which is handled in handlers.py
            btn.append([InlineKeyboardButton(text="Close", callback_data=f'close_suggestion|{user_id}')]) 
            
            sent_message = await message.reply(
                f"🎬 Found {len(unique_names)} results for **{query}**.\nSelect the correct series below:",
                reply_markup=InlineKeyboardMarkup(btn),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            
            # Use a shorter auto-delete for groups to reduce spam (60 seconds)
            asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, 60))
            
            return
        
        # --- Display Single Search Result (Most likely result is unique_names[0]) ---
        series_to_display = unique_names[0]
        await display_series_menu(cyber, message, series_to_display, is_callback=False)


    # --- Group Suggestion/Selection Callbacks ---

    @cyber.on_callback_query(filters.regex(r"^gsugg_select\|"))
    async def group_sugg_select_handler(cyber, query: CallbackQuery):
        """Handles selection from group search suggestions."""
        data = query.data.split("|", 2)
        if len(data) < 3:
            await query.answer("❌ Error: Invalid suggestion data.", show_alert=True)
            return

        series_name = data[2]
        await query.answer(f"Selected: {series_name}...", cache_time=0)
        
        await display_series_menu(cyber, query.message, series_name, is_callback=True, callback_query=query)


    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def group_season_select_handler(cyber, query: CallbackQuery):
        """Handles season selection in group and shows the language menu (group version)."""
        data = query.data.split("|")
        if len(data) < 3:
            logger.error(f"Malformed callback data for group_season_select_handler: {query.data}")
            await query.answer("❌ Error: Invalid season selection data.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            logger.error(f"Callback data had non-integer season: {data[2]}")
            await query.answer("❌ Error: Invalid season number format.", show_alert=True)
            return

        await show_group_language_menu(cyber, query, series_name, season)
        await query.answer()


    @cyber.on_callback_query(filters.regex(r"^gls\|"))
    async def group_language_select_handler(cyber, query: CallbackQuery):
        """Handles language selection in group and shows the quality menu (group version)."""
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
        
        await show_group_quality_menu(cyber, query, series_name, language, season)
        await query.answer()

    
    @cyber.on_callback_query(filters.regex(r"^gqs\|"))
    async def group_quality_select_handler(cyber, query: CallbackQuery):
        """
        Handles quality selection in group.
        Redirects the user to the bot's PM for file delivery using a deep link.
        """
        data = query.data.split("|")
        if len(data) < 5:
            await query.answer("❌ Error: Invalid quality selection data.", show_alert=True)
            return

        series_name = data[1]
        season = data[2]
        language = data[3]
        quality = data[4]

        # Get bot info to generate the start link
        bot_info = await cyber.get_me()
        bot_username = bot_info.username

        # Construct the deep link to start the bot with the file request
        # Payload format: 'file_request|series|season|lang|quality'
        start_payload = f"file_request|{series_name}|{season}|{language}|{quality}"
        
        deeplink = f"https://t.me/{bot_username}?start={start_payload}"

        # Create the redirect button
        redirect_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📥 Get S{season} ({quality}/{language}) in PM 📥", 
                url=deeplink
            )]
        ])
        
        redirect_caption = (
            f"✅ **Redirecting to PM for Files**\n\n"
            f"**🚨 IMPORTANT:** If I don't send files, please go to my chat and click **'Start'** first!\n\n"
            f"Please click the button below to receive all episodes for:\n"
            f"**{series_name}** - Season **{season}**\n"
            f"Quality: **{quality}** | Language: **{language}**\n\n"
            f"**Note:** Files will be sent in my Private Chat."
        )

        try:
            await query.message.edit_caption(
                redirect_caption,
                reply_markup=redirect_keyboard,
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Failed to edit group message with redirect button: {e}. Falling back to edit_text.")
            try:
                await query.message.edit_text(
                    redirect_caption, 
                    reply_markup=redirect_keyboard, 
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception:
                pass

        await query.answer("Redirecting you to Private Message for files...", show_alert=False)


    # --- Helper Functions for Group Menus ---

    async def display_series_menu(cyber, message: Message, series_name: str, is_callback: bool, callback_query: CallbackQuery = None):
        """Displays the main search result/season selection menu in a group."""
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
                key = f"poster::{series_name.strip().lower()}"
                if key in db_cache:
                    poster_url = db_cache[key]
        except Exception:
            pass

        # Fetch Poster if needed
        if (not poster_url or poster_url == DEFAULT_POSTER) and imdb_enabled:
            try:
                poster_tmp = await get_series_poster_async(series_name)
                poster_url = poster_tmp or DEFAULT_POSTER
            except Exception as e:
                logger.warning(f"[OMDB FETCH FAIL - group_menu] {series_name} -> {e}")
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
            f"𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲d 𝗦𝗲𝗮𝘀𝗼𝗻 👇"
        )

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for season in summary.get("seasons", []):
            # Use 'gss' for group season select callback
            callback_data = f"gss|{series_name_short}|{season}"
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)

        # Handle message editing if it's a callback, or sending a new message if it's a new search
        if is_callback and callback_query:
            try:
                await callback_query.message.edit_media(
                    media=InputMediaPhoto(media=poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                    reply_markup=reply_markup,
                )
            except MessageNotModified:
                pass
            except Exception as e:
                logger.error(f"Failed to edit media for group search result: {e}. Falling back to edit_text.")
                try:
                    await callback_query.message.edit_text(
                        response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN
                    )
                except Exception:
                    pass
            sent_message = callback_query.message
        else:
            # New message
            try:
                sent_message = await message.reply_photo(
                    photo=poster_url,
                    caption=response,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error(f"Failed to reply with photo for group search: {e}. Falling back to reply_text.")
                sent_message = await message.reply_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)

        # Use a shorter auto-delete for groups to reduce spam (60 seconds)
        asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, 60))


    async def show_group_language_menu(cyber: Client, query: CallbackQuery, series_name: str, season: int):
        """Displays language menu for a group chat."""
        languages = await get_unique_languages_by_season(series_name, season)
        if not languages:
            await query.answer(f"❌ No languages found for {series_name} S{season}.", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for lang in languages:
            # Callback: gls|series_name_short|season|lang (Group Language Select)
            callback_data = f"gls|{series_name_short}|{season}|{lang}"
            display_text = f"🔸 {lang} 🔸"
            keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])

        # Back to Season Select (Group version)
        keyboard.append([InlineKeyboardButton(
            "↺ Bᴀᴄᴋ Tᴏ Sᴇᴀsᴏɴ ↻", callback_data=f"gss|{series_name_short}|{season}")
        ])
        reply_markup = InlineKeyboardMarkup(keyboard)

        caption = (
            f"📺 Title **{series_name}** — Season **{season}**\n\n"
            f"🌐 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲d 𝗔𝘂𝗱𝗶𝗼 𝗟𝗮𝗻𝗴𝘂𝗮𝗴𝗲 👇:"
        )

        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in show_group_language_menu: {e}")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass

        await query.answer()

    
    async def show_group_quality_menu(cyber: Client, query: CallbackQuery, series_name: str, language: str, season: int):
        """Displays quality menu for a group chat."""
        qualities = await get_unique_qualities_by_lang_season(series_name, language, season)
        if not qualities:
            await query.answer(f"❌ No quality files found for S{season} ({language}).", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for quality in qualities:
            # Callback: gqs|series_name_short|season|language|quality (Group Quality Select)
            callback_data = f"gqs|{series_name_short}|{season}|{language}|{quality}"
            keyboard.append([InlineKeyboardButton(f"🔹 {quality} 🔹", callback_data=callback_data)])

        # Back to Language Select (Group version)
        keyboard.append([InlineKeyboardButton(
            "↺ Bᴀᴄᴋ Tᴏ Lᴀɴɢᴜᴀɢᴇs ↻", callback_data=f"gls|{series_name_short}|{season}|{language}")
        ])
        reply_markup = InlineKeyboardMarkup(keyboard)

        caption = (
            f"🎬 **{series_name}** – Season **{season}** ({language})\n\n"
            f"📌 Quality Selector\n\n"
            f"Tap a quality below to receive all episodes in my **Private Chat**.\n\n"
            f"**Note:** This ensures smooth, non-spammy delivery."
        )

        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in show_group_quality_menu: {e}")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                pass

        await query.answer()

    
    # Returning the handlers for registration
    return [
        group_search_handler, group_sugg_select_handler, 
        group_season_select_handler, group_language_select_handler,
        group_quality_select_handler
    ]
