from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta

scheduler = AsyncIOScheduler()


def start_scheduler():
    """
    Start scheduler safely after event loop exists.
    Call this once in main.py after await cyber.start()
    """
    try:
        if not scheduler.running:
            scheduler.start()
    except RuntimeError:
        pass


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
