import asyncio
import re
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified, ChannelInvalid, UsernameInvalid, UsernameNotModified, ChatAdminRequired
from pyrogram.errors.exceptions.flood_420 import FloodWait

from config import CHANNELS, media_filter, ADMINS, LOG_CHANNEL, lock, temp, logger
from database import save_file_to_db, get_bot_state, save_bot_state
from utils import parse_series_info, save_series_file_for_bulk_index

# The app object needs to be passed or accessed via a global scope in the main script.
# We'll assume the app is passed as the first argument (bot) in the handlers.

###################################################
## REAL-TIME FILE HANDLER (INDEXING LOGIC) ##
###################################################

def register_media_handler(app: Client):
    @app.on_message(filters.chat(CHANNELS) & media_filter)
    async def media_handler(_, message):
        """
        Handles new media messages in the configured channels and indexes them if they are a series file.
        """
        file_name = ""
        if message.document:
            file_name = message.document.file_name or ""
        elif message.video:
            file_name = message.video.file_name or ""
        else: 
            file_name = getattr(message.audio, "file_name", "")

        normalized_name, season, episode = parse_series_info(file_name)
        
        if season > 0:
            file_doc = {
                "channel_id": message.chat.id,
                "message_id": message.id,
                "file_name": file_name,
                "normalized_name": normalized_name,
                "season": season,
                "episode": episode,
                "timestamp": message.date
            }
            
            saved = await save_file_to_db(file_doc)
            if saved:
                logger.info(f"INDEXING: {normalized_name} S{season:02d}E{episode:02d} (Msg ID: {message.id})")
        else:
            logger.info(f"SKIP: Non-series file skipped ('{file_name}' from Msg ID: {message.id})")

    return media_handler

###################################################
## BULK INDEXING FUNCTIONS ##
###################################################

async def index_files_to_db(lst_msg_id, chat, msg, bot):
    total_files = 0
    duplicate = 0
    errors = 0
    deleted = 0
    no_media = 0
    unsupported = 0
    
    # 1. Check for a saved resume point
    resume_id = await get_bot_state(chat)
    if resume_id and resume_id < lst_msg_id:
        current_offset_id = resume_id
        await msg.reply_text(f"⚠️ **Resuming Indexing** from saved point: Message ID <code>{resume_id}</code>.", 
                             parse_mode=enums.ParseMode.HTML)
    else:
        current_offset_id = lst_msg_id
    
    async with lock:
        try:
            temp.CANCEL = False
            current_processed_count = 0
            
            while True: # <--- OUTER LOOP
                if temp.CANCEL:
                    await save_bot_state(chat, 1)  # Clear the resume point on cancel/completion
                    await msg.edit(f"Successfully Cancelled!!\n\nSaved <code>{total_files}</code> files to dataBase!\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>")
                    break
                    
                # --- START: Inner Try/Except block for FloodWait Handling ---
                try: 
                    # Determine the message IDs to fetch in this batch (100 messages)
                    start_id = max(1, current_offset_id - 100)
                    
                    # Fetch messages using a range of message IDs
                    messages = await bot.get_messages(
                        chat_id=chat, 
                        message_ids=list(range(start_id, current_offset_id)),
                    )
                    
                    if not messages:
                        break # Reached the beginning of the chat

                    # Sort messages in reverse order (highest ID first) since we iterate backward
                    messages.sort(key=lambda m: m.id, reverse=True)

                    for message in messages:
                        if temp.CANCEL:
                            break

                        current_processed_count += 1
                        
                        # Save state every 50 messages to ensure resume point is recent
                        if current_processed_count % 50 == 0:
                            # Save the ID BEFORE the current message (to avoid re-processing the current message)
                            await save_bot_state(chat, message.id + 1) 
                            
                            can = [[InlineKeyboardButton('🛑 Stop Indexing', callback_data='index_cancel')]]
                            reply = InlineKeyboardMarkup(can)
                            await msg.edit_text(
                                text=f"Total messages fetched: <code>{current_processed_count}</code>\n**RESUME POINT:** <code>{message.id + 1}</code>\nTotal messages saved: <code>{total_files}</code>\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>",
                                reply_markup=reply)

                        # --- Skipping/Resuming Logic (Original temp.CURRENT now only for manual skip)---
                        if temp.CURRENT > 1 and message.id > temp.CURRENT:
                            continue

                        temp.CURRENT = 1 # Reset manual skip
                        
                        # --- START: Series Indexing Logic ---
                        if message.empty:
                            deleted += 1
                            continue
                        elif not message.media:
                            no_media += 1
                            continue
                        elif message.media not in [enums.MessageMediaType.VIDEO, enums.MessageMediaType.AUDIO, enums.MessageMediaType.DOCUMENT]:
                            unsupported += 1
                            continue

                        is_saved, status_code = await save_series_file_for_bulk_index(message)
                        
                        if is_saved:
                            total_files += 1
                        elif status_code == 0:
                            duplicate += 1
                        elif status_code == 2:
                            errors += 1
                        # --- END: Series Indexing Logic ---
                        
                    # Break if canceled during message processing
                    if temp.CANCEL:
                        break
                        
                    # Set the offset for the next batch to the lowest ID in the current batch
                    current_offset_id = messages[-1].id if messages else 0
                    
                    # --- FIX: Add a small delay to avoid FloodWait ---
                    await asyncio.sleep(0.5) 
                    # --- END FIX ---
                    
                    # Check if we have processed all messages (message ID 1 is the first)
                    if current_offset_id <= 1:
                        break

                except FloodWait as e: # <--- CATCHES FLOOD_WAIT ONLY
                    logger.error(f"Hit FloodWait: Waiting for {e.value} seconds.")
                    # Save state before waiting to ensure continuity after restart
                    await save_bot_state(chat, current_offset_id) 
                    
                    await msg.edit(f'⚠️ FloodWait hit! Pausing indexing for **{e.value}** seconds to comply with Telegram API limits. The bot can **resume automatically** from ID <code>{current_offset_id}</code> if it restarts.')
                    await asyncio.sleep(e.value + 1)
                    continue
                
                except Exception as e: # <--- CATCHES OTHER ERRORS IN THE LOOP
                    logger.exception(e)
                    await msg.edit(f'Error: {e}')
                    break # Exit the while loop on unexpected error

        except Exception as e:
            # This outer try/except catches errors from the lock or outside the while loop's try block.
            logger.exception(e)
            await msg.edit(f'Fatal Error: {e}')
        else:
            # Clear the resume point on successful completion
            await save_bot_state(chat, 1) 
            await msg.edit(f'✅ Succesfully saved <code>{total_files}</code> to dataBase!\nDuplicate Files Skipped: <code>{duplicate}</code>\nDeleted Messages Skipped: <code>{deleted}</code>\nNon-Media messages skipped: <code>{no_media + unsupported}</code>(Unsupported Media - `{unsupported}` )\nErrors Occurred: <code>{errors}</code>')


def register_bulk_index_handlers(app: Client):

    @app.on_message(filters.command('setskip') & filters.user(ADMINS))
    async def set_skip_number(bot, message):
        if ' ' in message.text:
            _, skip = message.text.split(" ")
            try:
                skip = int(skip)
            except:
                return await message.reply("Skip number should be an integer.")
            await message.reply(f"Successfully set SKIP number as {skip}")
            temp.CURRENT = int(skip)
        else:
            await message.reply("Give me a skip number")

    @app.on_message((filters.forwarded | (filters.regex("(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")) & filters.text ) & filters.private & filters.incoming)
    async def send_for_index(bot, message):
        chat_id = None
        last_msg_id = None
        
        if message.text:
            regex = re.compile("(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
            match = regex.match(message.text)
            if not match:
                return await message.reply('Invalid link')
            chat_id_str = match.group(4)
            last_msg_id = int(match.group(5))
            
            if chat_id_str.isnumeric():
                chat_id = int(("-100" + chat_id_str))
            else:
                chat_id = chat_id_str 
                
        elif message.forward_from_chat and message.forward_from_chat.type == enums.ChatType.CHANNEL:
            last_msg_id = message.forward_from_message_id
            chat_id = message.forward_from_chat.username or message.forward_from_chat.id
        else:
            return 

        try:
            await bot.get_chat(chat_id)
        except ChannelInvalid:
            return await message.reply('This may be a private channel / group. Make me an admin over there to index the files.')
        except (UsernameInvalid, UsernameNotModified):
            return await message.reply('Invalid Link specified.')
        except Exception as e:
            logger.exception(e)
            return await message.reply(f'Errors - {e}')
            
        try:
            k = await bot.get_messages(chat_id, last_msg_id)
        except Exception:
            return await message.reply('Make Sure That Iam An Admin In The Channel, if channel is private, and the message ID exists.')
        if k.empty:
            return await message.reply('This may be group and iam not a admin of the group or the message ID is too high.')

        if message.from_user.id in ADMINS:
            buttons = [
                [
                    InlineKeyboardButton('Yes',
                                         callback_data=f'index#accept#{chat_id}#{last_msg_id}#{message.from_user.id}')
                ],
                [
                    InlineKeyboardButton('close', callback_data='close_data'),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(buttons)
            return await message.reply(
                f'Do you Want To Index This Channel/ Group ?\n\nChat ID/ Username: <code>{chat_id}</code>\nLast Message ID: <code>{last_msg_id}</code>',
                reply_markup=reply_markup)

        if type(chat_id) is int:
            try:
                link = (await bot.create_chat_invite_link(chat_id)).invite_link
            except ChatAdminRequired:
                return await message.reply('Make sure iam an admin in the chat and have permission to invite users.')
            except Exception:
                link = "N/A" 
        else:
            link = f"@{message.forward_from_chat.username}"
        buttons = [
            [
                InlineKeyboardButton('Accept Index',
                                         callback_data=f'index#accept#{chat_id}#{last_msg_id}#{message.from_user.id}')
            ],
            [
                InlineKeyboardButton('Reject Index',
                                         callback_data=f'index#reject#{chat_id}#{message.id}#{message.from_user.id}'),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        await bot.send_message(LOG_CHANNEL,
                               f'#IndexRequest\n\nBy : {message.from_user.mention} (<code>{message.from_user.id}</code>)\nChat ID/ Username - <code> {chat_id}</code>\nLast Message ID - <code>{last_msg_id}</code>\nInviteLink - {link}',
                               reply_markup=reply_markup)
        await message.reply('ThankYou For the Contribution, Wait For My Moderators to verify the files.')


    @app.on_callback_query(filters.regex(r'^index'))
    async def index_files_callback(bot, query):
        if query.data.startswith('index_cancel'):
            temp.CANCEL = True
            return await query.answer("Stopping Indexing...")
        _, raju, chat, lst_msg_id, from_user = query.data.split("#")
        if raju == 'reject':
            await query.message.delete()
            await bot.send_message(int(from_user),
                                  f'Your Submission for indexing {chat} has been decliened by our moderators.',
                                   reply_to_message_id=int(lst_msg_id))
            return

        if lock.locked():
            return await query.answer('Wait until previous process complete.', show_alert=True)
        msg = query.message

        await query.answer('Processing...⏳', show_alert=True)
        if int(from_user) not in ADMINS:
            await bot.send_message(int(from_user),
                                  f'Your Submission for indexing {chat} has been accepted by our moderators and will be added soon.',
                                   reply_to_message_id=int(lst_msg_id))
        await msg.edit(
            "Starting Indexing",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton('🛑 Stop Indexing', callback_data='index_cancel')]]
            )
        )
        try:
            chat = int(chat)
        except ValueError:
            pass 
        except Exception:
            pass 

        await index_files_to_db(int(lst_msg_id), chat, msg, bot)
    
    return [send_for_index, index_files_callback, set_skip_number]
