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
    GROUP_FILTER_ENABLED_CHATS, # Assuming you will add this to config.py for authorized group IDs
)
from utils import (
    clean_series_name,
    delete_message_later,
)
from .poster import get_series_poster_async # Assuming poster.py is in the same directory

# --- Group Search Handlers ---

def register_group_handlers(cyber: Client):

    # A simple checker to ensure the chat is authorized for group filtering
    async def is_authorized_group(_, __, message: Message):
        """Checks if the message is from an authorized group chat ID."""
        # Use message.chat.id, not message.from_user.id
        # NOTE: GROUP_FILTER_ENABLED_CHATS must be defined in config.py as a list/tuple of chat IDs
        if GROUP_FILTER_ENABLED_CHATS and message.chat.id not in GROUP_FILTER_ENABLED_CHATS:
            return False
        return message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)


    @cyber.on_message(filters.text & filters.group & ~filters.command(["start", "index"]) & filters.create(is_authorized_group))
    async def group_search_handler(cyber, message: Message):
        """Handles text messages in a group for series search."""
        query = message.text.strip()
        # Ignore replies, very short queries, or queries likely meant for the bot command indexer
        if message.reply_to_message or len(query.split()) < 2:
            return

        # Perform the search
        try:
            all_found_names = await search_series(query)
        except Exception as e:
            logger.warning(f"[GROUP SEARCH ERROR] search_series failed for query `{query}`: {e}")
            return # Fail silently in groups

        unique_names = list(dict.fromkeys([clean_series_name(name) for name in all_found_names]))

        if not unique_names:
            # Fallback/final check: try cleaning the query
            clean_query = re.sub(
                r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\sme)?)|movie(s)?|new|latest|br((o|u)h?)*|^h(e|a)?(l)*(o)*|mal(ayalam)?|t(h)?amil|file|that|find|und(o)*|kit(t(i|y)?)?o(w)?|thar(u)?(o)*w?|kittum(o)*|aya(k)*(um(o)*)?|full\smovie|any(one)|with\ssubtitle(s)?)",
                "", message.text, flags=re.IGNORECASE).strip()

            if clean_query:
                try:
                    unique_names = list(dict.fromkeys([clean_series_name(name) for name in await search_series(clean_query)]))
                except Exception:
                    pass

            if not unique_names:
                # No results, silently ignore
                return

        # --- Display Suggestions or Single Result ---
        
        if len(unique_names) > 1:
            # Display suggestions
            movielist = unique_names[:5] # Show top 5 suggestions
            
            btn = [[
                InlineKeyboardButton(
                    text=movie,
                    # Using 'g_sugg_select' handler for all suggestions
                    # Callback: g_sugg_select|chat_id|message_id|series_name
                    callback_data=f"g_sugg_select|{message.chat.id}|{message.id}|{movie}", 
                )
            ] for movie in movielist]
            
            btn.append([InlineKeyboardButton(text="Close", callback_data=f'close_group|{message.chat.id}|{message.id}')])
            
            sent_message = await message.reply_text(
                f"I found {len(unique_names)} results for `<code>{query}</code>`.\nDid you mean any one of these?",
                reply_markup=InlineKeyboardMarkup(btn),
                parse_mode=enums.ParseMode.HTML
            )
            
        else:
            # Display single result
            series_to_display = unique_names[0]
            sent_message = await _display_series_summary(cyber, message, series_to_display)
        
        # Add auto-delete logic if applicable (assuming you have a timer setting)
        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 0)
        if auto_delete_timer and int(auto_delete_timer) > 0 and sent_message:
            asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, int(auto_delete_timer)))

        await message.delete() # Delete the user's search query for cleanup


    async def _display_series_summary(cyber, message: Message, series_name: str, edit_message: bool = False):
        """Fetches and displays the series summary/season menu in the group chat."""
        
        poster_url = DEFAULT_POSTER
        imdb_enabled = True
        try:
            imdb_enabled = await get_global_setting("IMDB_POSTER_ENABLED", True)
        except Exception:
            pass

        # Poster logic (check cache, then fetch)
        try:
            cache_file = OMDB_CACHE_DB or "/tmp/omdb_cache.db"
            with shelve.open(cache_file) as db_cache:
                key = f"poster::{series_name.strip().lower()}"
                if key in db_cache:
                    poster_url = db_cache[key]
        except Exception:
            pass

        if (not poster_url or poster_url == DEFAULT_POSTER) and imdb_enabled:
            try:
                poster_tmp = await get_series_poster_async(series_name)
                poster_url = poster_tmp or DEFAULT_POSTER
            except Exception as e:
                logger.warning(f"[OMDB FETCH FAIL - group_search] {series_name} -> {e}")
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
            # Group Callback: g_ss|series_name_short|season
            callback_data = f"g_ss|{series_name_short}|{season}"
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)

        if edit_message and isinstance(message, CallbackQuery):
            try:
                # Use message.message for the original message object from a callback query
                await message.message.edit_media(
                    media=InputMediaPhoto(media=poster_url, caption=response, parse_mode=enums.ParseMode.MARKDOWN),
                    reply_markup=reply_markup,
                )
                return message.message
            except MessageNotModified:
                return message.message
            except Exception as e:
                logger.error(f"Failed to edit media for group search result: {e}. Falling back to edit_text.")
                try:
                    await message.message.edit_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
                    return message.message
                except Exception:
                    pass
        elif not edit_message:
            try:
                sent_message = await message.reply_photo(
                    photo=poster_url,
                    caption=response,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
                return sent_message
            except Exception as e:
                logger.error(f"Failed to reply with photo for group search result: {e}. Falling back to reply_text.")
                sent_message = await message.reply_text(response, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
                return sent_message
        return None


    # --- Group Suggestion Callbacks ---

    @cyber.on_callback_query(filters.regex(r"^g_sugg_select\|"))
    async def group_sugg_select_handler(cyber, query: CallbackQuery):
        """Handles selection from search suggestions in a group."""
        data = query.data.split("|", 3)
        if len(data) < 4:
            await query.answer("❌ Error: Invalid suggestion data.", show_alert=True)
            return

        # data[1] is chat_id, data[2] is message_id (of the original search query - NOT USED)
        series_name = data[3]
        await query.answer(f"Selected: {series_name}...", cache_time=0)

        await _display_series_summary(cyber, query, series_name, edit_message=True)
        # Note: Auto-delete of the new message is handled by the initial search_handler cleanup.


    @cyber.on_callback_query(filters.regex(r"^close_group\|"))
    async def close_group_suggestion_handler(cyber, query: CallbackQuery):
        """Handles closing the suggestion message in a group."""
        try:
            # Delete the suggestion message itself
            await cyber.delete_messages(query.message.chat.id, query.message.id)
        except Exception as e:
            logger.warning(f"Failed to delete group suggestion message: {e}")
            await query.answer("❌ Failed to close message.", show_alert=True)
            return
        await query.answer("Closed.", show_alert=False)


    # --- Group Selection Callbacks ---

    @cyber.on_callback_query(filters.regex(r"^g_ss\|"))
    async def group_season_select_handler(cyber, query: CallbackQuery):
        """Handles season selection in a group and shows the language menu."""
        data = query.data.split("|")
        if len(data) < 3:
            await query.answer("❌ Error: Invalid season selection data.", show_alert=True)
            return

        series_name = data[1]
        try:
            season = int(data[2])
        except ValueError:
            await query.answer("❌ Error: Invalid season number format.", show_alert=True)
            return
        
        # Group Language Menu
        await query.answer("Fetching languages...", cache_time=0)
        languages = await get_unique_languages_by_season(series_name, season)
        if not languages:
            await query.answer(f"❌ No languages found for {series_name} S{season}.", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for lang in languages:
            # Callback: g_ls|series_name_short|season|lang
            callback_data = f"g_ls|{series_name_short}|{season}|{lang}"
            display_text = f"🔸 {lang} 🔸"
            keyboard.append([InlineKeyboardButton(display_text, callback_data=callback_data)])

        # Back to Search Summary: g_back_to_search|series_name_short
        keyboard.append([InlineKeyboardButton("↺ Bᴀᴄᴋ Tᴏ Sᴇᴀʀᴄʜ ↺", callback_data=f"g_back_to_search|{series_name_short}")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        caption = (
            f"📺 Title **{series_name}** — Season **{season}**\n\n"
            f"🌐 𝗣𝗹𝗲𝗮𝘀𝗲 𝘀𝗲𝗹𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗽𝗿𝗲𝗳𝗲𝗿𝗿𝗲𝗱 𝗔𝘂𝗱𝗶𝗼 𝗟𝗮𝗻𝗴𝘂𝗮𝗴𝗲 👇:"
        )

        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in group_season_select_handler: {e}")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except MessageNotModified:
                pass
            except Exception:
                pass
        
        await query.answer()


    @cyber.on_callback_query(filters.regex(r"^g_ls\|"))
    async def group_language_select_handler(cyber, query: CallbackQuery):
        """Handles language selection in a group and shows the quality menu."""
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

        # Group Quality Menu
        await query.answer("Fetching qualities...", cache_time=0)
        qualities = await get_unique_qualities_by_lang_season(series_name, language, season)
        if not qualities:
            await query.answer(f"❌ No quality files found for S{season} ({language}).", show_alert=True)
            return

        keyboard = []
        series_name_short = series_name[:MAX_CALLBACK_LENGTH]
        for quality in qualities:
            # THIS BUTTON REDIRECTS TO PM: switch_inline_query_current_chat
            # The inline query will be used by the PM search handler logic for direct file sending.
            # We construct a unique query to be handled by the bot's PM search.
            # Query format for PM search to trigger file send: /getfile|<series>|<season>|<lang>|<quality>
            pm_query = f"/getfile|{series_name_short}|{season}|{language}|{quality}"
            
            keyboard.append([InlineKeyboardButton(f"📥 {quality} - Get Season Pack 📥", switch_inline_query=pm_query)])

        # Back to Language: g_bl|series_name_short|season
        keyboard.append([InlineKeyboardButton("↺ Bᴀᴄᴋ Tᴏ Lᴀɴɢᴜɢᴇs ↺", callback_data=f"g_bl|{series_name_short}|{season}")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        caption = (
            f"🎬 **{series_name}** – Season **{season}** ({language})\n\n"
            f"📌 **Quality Selector**\n\n"
            f"Tap the **Get Season Pack** button below.\n"
            f"You will be redirected to the bot's PM to download all episodes instantly!"
        )

        try:
            await query.message.edit_caption(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
        except Exception as e:
            logger.error(f"Error editing message caption in group_language_select_handler: {e}")
            try:
                await query.message.edit_text(caption, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            except MessageNotModified:
                pass
            except Exception:
                pass
        
        await query.answer()


    # --- Group Back Handlers ---

    @cyber.on_callback_query(filters.regex(r"^g_bl\|"))
    async def group_back_to_language_handler(cyber, query: CallbackQuery):
        """Handles going back to the language selection menu in a group."""
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

        # Re-use the language menu logic from group_season_select_handler
        await group_season_select_handler(cyber, query)
        await query.answer()

    @cyber.on_callback_query(filters.regex(r"^g_back_to_search\|"))
    async def group_back_to_search_handler(cyber, query: CallbackQuery):
        """Handles going back to the main series summary/season selection menu in a group."""
        data = query.data.split("|")
        if len(data) < 2:
            await query.answer("❌ Error: Invalid back data.", show_alert=True)
            return

        series_name = data[1]
        
        # Re-display the main summary
        await _display_series_summary(cyber, query, series_name, edit_message=True)
        await query.answer()


    return [
        group_search_handler, group_sugg_select_handler, close_group_suggestion_handler,
        group_season_select_handler, group_language_select_handler,
        group_back_to_language_handler, group_back_to_search_handler,
    ]

# --- Note on integrating with PM handlers ---
# To complete the functionality, you need to modify your PM search handler (in handlers.py) 
# to detect the special inline query format: "/getfile|<series>|<season>|<lang>|<quality>".

# In your existing handlers.py:
# 1. Update the 'search_handler' to check for a message.text starting with '/getfile|'
# 2. If it is a /getfile message, parse the data and directly call the quality_select_handler's core logic
#    (or better, refactor quality_select_handler to accept the data directly and call its core file-sending logic).
