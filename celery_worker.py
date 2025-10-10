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
# (Optional) smoother queueing for long jobs
celery_app.conf.worker_prefetch_multiplier = 1

async def collect_lines(url: str, whisper_model="tiny", cb=None) -> str:
    """Iterate async generator; call cb(idx) every 20 lines for progress."""
    buf, idx = [], 0
    async for line in process_cvtv_stream(url, whisper_model=whisper_model):
        buf.append(line)
        idx += 1
        if cb and idx % 20 == 0:
            cb(idx)
    return "\n".join(buf)

@celery_app.task(bind=True, track_started=True)
def whisper_fallback_task(self, url: str) -> str:
    """
    PENDING -> STARTED (auto) -> PROGRESS (periodic) -> SUCCESS/FAILURE
    Returns full transcript (string) on success.
    """
    task_id = self.request.id
    try:
        if ".cvtv.org" in url:
            def report(idx: int):
                self.update_state(
                    state="PROGRESS",
                    meta={"msg": f"Whisper transcription in progress ({idx} lines)"}
                )
            result = asyncio.run(collect_lines(url, whisper_model="tiny", cb=report))
        else:
            # Lightweight path (non-CVTV). One progress ping for UI.
            self.update_state(state="PROGRESS", meta={"msg": "Fetching transcript..."})
            result = asyncio.run(fallback_to_whisper_html(url, whisper_model="tiny"))

        # ✅ Update Mongo document once done
        transcripts_collection.update_one(
            {"task_id": task_id},
            {"$set": {
                "status": "COMPLETED",
                "transcript": result or "[Error] Whisper returned no text.",
                "completed_at": datetime.datetime.utcnow()
            }}
        )

        return result or "[Error] Whisper returned no text."
    except Exception as e:
        # ❌ On failure
        transcripts_collection.update_one(
            {"task_id": task_id},
            {"$set": {
                "status": "FAILED",
                "error": str(e),
                "completed_at": datetime.datetime.utcnow()
            }}
        )
        logging.error(f"[{task_id}] Whisper task failed: {e}")
        raise RuntimeError(f"Whisper task failed: {e}")

def cancel_task(task_id, terminate=True):
    """
    Cancel a Celery task by ID.
    """
    celery_app.control.revoke(task_id, terminate=terminate, signal="SIGKILL")