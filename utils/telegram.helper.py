import asyncio


async def delete_message_later(cyber, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await cyber.delete_messages(chat_id, message_id)
    except:
        pass
