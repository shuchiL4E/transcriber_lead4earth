import multiprocessing
multiprocessing.set_start_method("spawn", force=True)
import os
import logging
import asyncio
from app.scraper import process_cvtv_stream, fallback_to_whisper_html
from celery import Celery
from dotenv import load_dotenv
import datetime
from app.db import transcripts_collection

load_dotenv()

# Logging
logging.basicConfig(
    filename="celery.log",
    filemode="a",
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)

redis_url = os.getenv("REDIS_URL")
mongo_uri = os.getenv("MONGO_URI")

# Broker/backend
celery_app = Celery("tasks", broker=redis_url, backend=redis_url)

# Ensure STARTED is reported
celery_app.conf.task_track_started = True
celery_app.conf.worker_prefetch_multiplier = 1  # For smoother queueing


# ---------------- Utility helpers ----------------

def run_async(coro):
    import nest_asyncio
    nest_asyncio.apply()

    loop = asyncio.get_event_loop()
    task = loop.create_task(coro)

    if loop.is_running():
        while not task.done():
            loop.stop()
            loop.run_forever()
        return task.result()
    else:
        return loop.run_until_complete(task)


async def collect_lines(url: str, whisper_model="tiny", cb=None) -> str:
    """Iterate async generator; call cb(idx) every 20 lines for progress."""
    buf, idx = [], 0
    async for line in process_cvtv_stream(url, whisper_model=whisper_model):
        buf.append(line)
        idx += 1
        if cb and idx % 20 == 0:
            cb(idx)
    return "\n".join(buf)


# ---------------- Celery Task ----------------

@celery_app.task(bind=True, track_started=True)
def whisper_fallback_task(self, url: str, meeting_id: str) -> str:
    """
    Celery fallback task for Whisper transcription.
    Uses meeting_id to update Mongo record instead of task_id.
    """
    try:
        logging.info(f"[Whisper Task] Started for meeting_id={meeting_id}, url={url}")

        if ".cvtv.org" in url:
            def report(idx: int):
                self.update_state(
                    state="PROGRESS",
                    meta={"msg": f"Whisper transcription in progress ({idx} lines)"}
                )
            result = run_async(collect_lines(url, whisper_model="tiny", cb=report))
        else:
            self.update_state(state="PROGRESS", meta={"msg": "Fetching transcript..."})
            result = run_async(fallback_to_whisper_html(url, whisper_model="tiny"))

        # ✅ Update Mongo document by meeting_id
        transcripts_collection.update_one(
            {"meeting_id": meeting_id},
            {"$set": {
                "transcript": result or "[Error] Whisper returned no text.",
                "status": "COMPLETED",
                "updated_at": datetime.datetime.utcnow(),
            }},
            upsert=True
        )

        logging.info(f"[Whisper Task] Completed for meeting_id={meeting_id}")
        return {"meeting_id": meeting_id, "status": "COMPLETED"}

    except Exception as e:
        # ❌ On failure
        transcripts_collection.update_one(
            {"meeting_id": meeting_id},
            {"$set": {
                "status": "FAILED",
                "error": str(e),
                "updated_at": datetime.datetime.utcnow(),
            }},
            upsert=True
        )
        logging.error(f"[Whisper Task] Failed for meeting_id={meeting_id}: {e}")
        raise RuntimeError(f"Whisper task failed for {meeting_id}: {e}")


# ---------------- Cancel Helper ----------------

def cancel_task(task_id, terminate=True):
    """
    Cancel a Celery task by ID.
    """
    celery_app.control.revoke(task_id, terminate=terminate, signal="SIGKILL")
