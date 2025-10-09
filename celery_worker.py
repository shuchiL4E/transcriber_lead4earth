# celery_worker.py
import asyncio
from celery import Celery
from app.scraper import process_cvtv_stream, fallback_to_whisper_html

celery_app = Celery("tasks", broker="redis://localhost:6379/0", backend="redis://localhost:6379/0")

async def collect_lines(url: str, whisper_model="tiny", status_cb=None):
    """
    Consume async generator process_cvtv_stream() and collect transcript lines.
    """
    lines = []
    async for line in process_cvtv_stream(url, whisper_model=whisper_model, status_cb=status_cb):
        lines.append(line)
    return "\n".join(lines)

@celery_app.task(bind=True, track_started=True)
def whisper_fallback_task(self, url: str):
    """
    CVTV transcription pipeline with progress updates.
    """
    try:
        if ".cvtv.org" in url:

            def report(stage: str, url_ref=None):
                """Map scraper stage → human message"""
                mapping = {
                    "fetching_audio": "Step 1/4: Fetching audio from CVTV…",
                    "audio_downloaded": "Step 2/4: Audio downloaded successfully.",
                    "transcription_started": "Step 3/4: Transcription started.",
                    "transcription_completed": "Step 4/4: Transcription completed ✅",
                }
                #msg = mapping.get(stage, stage)
                msg = mapping.get(stage, f"[Unknown stage: {stage}]")

                # 🔔 Print and log everything for verification
                print(f"🚀 Triggering status_cb('{stage}')")
                print(f"[Celery report] Stage: {stage}, Message: {msg}")

                self.update_state(state="PROGRESS", meta={"msg": msg})



                #self.update_state(state="PROGRESS", meta={"msg": msg})

            # Run async generator + collect lines
            transcript = asyncio.run(
                collect_lines(url, whisper_model="tiny", status_cb=report)
            )

            return transcript

        #else:
            #self.update_state(state="PROGRESS", meta={"msg": "Running fallback transcript…"})
            #result = asyncio.run(fallback_to_whisper_html(url, whisper_model="tiny"))
            #return result or "[Error] No transcript found."
    
        else:
            # --- Fallback Path ---
            def report(stage: str, *_):
                """
                Receives progress messages from fallback_to_whisper_html()
                (if it yields stages internally)
                """
                mapping = {
                    "found_media": "Step 1/4: Found media source…",
                    "fetch_started": "Step 2/4: Fetching in progress ✅",
                    "transcription_started": "Step 3/4: Transcription started 🧠",
                    "transcription_completed": "Step 4/4: Transcription completed 🎉",
                }

                msg = mapping.get(stage, f"[Fallback] {stage}")
                print(f"[Fallback] {msg}")  # For backend logs
                self.update_state(state="PROGRESS", meta={"msg": msg})

            result = asyncio.run(
                fallback_to_whisper_html(url, whisper_model="tiny", status_cb=report)
            )

            return result or "[Error] No transcript found."
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        raise RuntimeError(f"Whisper task failed: {e}\n{tb}")
