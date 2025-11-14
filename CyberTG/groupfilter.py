# groupfilter.py
import asyncio
import re
import shelve
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery, # Added import for type hinting in the fix
)
# Assuming these are accessible from the execution environment
from DB.database import (
    search_series,
    get_series_summary,
    get_global_setting,
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
    check_force_subscribe,
)

# --- Group-Specific Handlers ---

def register_group_handlers(cyber: Client):
    """
    Registers group handlers and defines a utility to fetch the bot's username dynamically.
    """
    
    # 1. Variable to cache the username
    bot_username_cache = None 

    async def get_bot_username():
        """Fetches the bot's username once and caches it."""
        nonlocal bot_username_cache
        if bot_username_cache is None:
            me = await cyber.get_me()
            bot_username_cache = me.username
        return bot_username_cache

        
    @cyber.on_message(filters.text & filters.group & ~filters.command(["start", "help", "settings"]))
    async def group_search_handler(cyber, message: Message):
        # ... (group_search_handler content remains the same) ...
        # (Only excerpting the parts related to bot_username for brevity)
        
        if message.text.startswith("/") and message.text.lower() not in ["/search"]:
            return

        query = message.text.strip()
        
        if len(query) < 3:
            return

        try:
            all_found_names = await search_series(query) 
        except Exception as e:
            logger.warning(f"[GROUP SEARCH ERROR] search_series failed for query `{query}`: {e}")
            all_found_names = []
            
        unique_names = list(dict.fromkeys([clean_series_name(name) for name in all_found_names]))

        if not unique_names:
            sent_message = await message.reply_text(
                f"❌ No series found for `<code>{query}</code>` in the database.", 
                parse_mode=enums.ParseMode.HTML,
                quote=True
            )
            auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 60) 
            if auto_delete_timer and int(auto_delete_timer) > 0:
                asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, int(auto_delete_timer)))
            return

        # --- Display Suggestions or Single Result ---
        
        if len(unique_names) > 1:
            movielist = unique_names[:5]
            user_id = message.from_user.id
            
            btn = [[
                InlineKeyboardButton(
                    text=movie,
                    callback_data=f"sugg_select|{user_id}|{movie}", 
                )
            ] for movie in movielist]
            
            btn.append([InlineKeyboardButton("❌ Close Suggestions", callback_data=f'close_suggestion|{user_id}')])
            
            sent_message = await message.reply(
                f"I found {len(unique_names)} results for `<code>{query}</code>`.\nDid you mean any one of these?",
                reply_markup=InlineKeyboardMarkup(btn),
                parse_mode=enums.ParseMode.HTML,
                quote=True
            )
            
            auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 60)
            if auto_delete_timer and int(auto_delete_timer) > 0:
                asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, int(auto_delete_timer)))
            
            return
        
        # --- Display Single Search Result ---
        series_to_display = unique_names[0]

        summary = await get_series_summary(series_to_display)

        languages_list = ", ".join(summary.get("languages", []))
        seasons_count = len(summary.get("seasons", []))
        qualities_list = ", ".join(summary.get("qualities", []))

        response = (
            f"**○ Title:** {series_to_display}\n"
            f"**○ Language:** {languages_list or 'N/A'}\n"
            f"**○ Season Available:** {seasons_count}\n"
            f"**○ Quality Available:** {qualities_list or 'N/A'}\n\n"
            f"**To get the files, please select a season below:** 👇"
        )

        keyboard = []
        series_name_short = series_to_display[:MAX_CALLBACK_LENGTH]
        for season in summary.get("seasons", []):
            callback_data = f"gss|{series_name_short}|{season}"
            keyboard.append([InlineKeyboardButton(f"Season {season}", callback_data=callback_data)])
        
        keyboard.append([InlineKeyboardButton("❌ Close Result", callback_data=f'close_suggestion|{message.from_user.id}')])

        reply_markup = InlineKeyboardMarkup(keyboard)

        sent_message = await message.reply_text(
            response, 
            reply_markup=reply_markup, 
            parse_mode=enums.ParseMode.MARKDOWN,
            quote=True
        )

        auto_delete_timer = await get_global_setting("AUTO_DELETE_TIMER", 60)
        if auto_delete_timer and int(auto_delete_timer) > 0:
            asyncio.create_task(delete_message_later(cyber, sent_message.chat.id, sent_message.id, int(auto_delete_timer)))


    # --- Group Callbacks (GSS) ---

    @cyber.on_callback_query(filters.regex(r"^gss\|"))
    async def group_season_select_handler(cyber, query: CallbackQuery):
        """
        Handles season selection in a group. Directs the user to PM to continue.
        """
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

        await query.answer("Continuing in Private Message...", show_alert=False)
        
        # Check Force Subscribe status now
        is_member, invite_link = await check_force_subscribe(cyber, query.from_user.id)
        
        if not is_member:
            try:
                # Send a message to PM telling them to join
                await cyber.send_message(
                    chat_id=query.from_user.id,
                    text="🛑 **Please join our channel to get the file menus.**\n\n"
                         "Click the button below to join:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 Join Channel", url=invite_link)]]),
                    parse_mode=enums.ParseMode.MARKDOWN,
                )
            except Exception:
                pass 
                
            await query.answer(
                "🛑 Please check your private message for instructions to continue (Force Subscribe required).", 
                show_alert=True
            )
            return

        # --- Get Bot Username and Direct to PM ---
        # FIX: Calling the utility function defined in the outer scope
        bot_username = await get_bot_username() 

        # Reusing the 'ss' (Season Select) callback prefix from handlers.py 
        start_pm_callback_data = f"ss|{series_name}|{season}" 

        try:
            # 1. Send initial message to user's PM
            await cyber.send_message(
                chat_id=query.from_user.id,
                text=(
                    f"**{series_name}** - Season **{season}**\n\n"
                    f"You selected this from the group. Please click **'Continue in PM'** "
                    f"to proceed with Language Selection."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        text="▶️ Continue in PM", 
                        callback_data=start_pm_callback_data
                    )
                ]]),
                parse_mode=enums.ParseMode.MARKDOWN
            )
            
            # 2. Update the group message (the original query message)
            await query.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        text="💬 Continue in PM",
                        url=f"t.me/{bot_username}?start=select_{series_name}_{season}" # Deep linking
                    )
                ]])
            )

        except Exception as e:
            logger.error(f"Failed to send PM to user {query.from_user.id}: {e}")
            await query.answer(
                "❌ Failed to send you a private message! Please start a chat with the bot first.", 
                show_alert=True
            )

    return [
        group_search_handler,
        group_season_select_handler
    ]
