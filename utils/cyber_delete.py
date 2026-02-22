from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta
import asyncio

scheduler = AsyncIOScheduler()


def start_scheduler():
    """
    Start scheduler safely after event loop is running.
    Call this once inside main.py after bot.start()
    """
    if not scheduler.running:
        scheduler.start()


async def delete_message_job(cyber, chat_id, message_id):
    try:
        await cyber.delete_messages(chat_id, message_id)
    except:
        pass


def delete_message_later(cyber, chat_id, message_id, delay):

    if delay <= 0:
        return

    run_time = datetime.now() + timedelta(seconds=delay)

    scheduler.add_job(
        delete_message_job,
        trigger=DateTrigger(run_date=run_time),
        args=[cyber, chat_id, message_id],
        misfire_grace_time=30
    )
