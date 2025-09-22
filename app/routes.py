# app/route.py
import asyncio
import os
import uuid
import time
import datetime
import logging
from flask import Blueprint, request, jsonify, Response, render_template, stream_with_context
from celery_worker import whisper_fallback_task
from .scraper import fetch_transcript_for_url, fetch_youtube_transcript
from .utils import extract_youtube_video_id

api_bp = Blueprint("api", __name__, template_folder="templates")

logging.basicConfig(
    filename="scraper.log",
    filemode="a",
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)

@api_bp.route("/gettranscript", methods=["GET"])
def get_form():
    return render_template("index.html")

@api_bp.route("/transcript", methods=["POST"])
def get_transcript():
    """
    SSE stream: only minimal status messages while running,
    and final transcript printed plainly.
    """
    url = (request.json.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")

    def sse_generate():
        start_time = datetime.datetime.now()
        pid = os.getpid()
        req_id = str(uuid.uuid4())[:8]

        try:
            if ".cvtv.org" in url:
                task = whisper_fallback_task.delay(url)
                last = None

                while not task.ready():
                    state = task.state
                    if state != last:
                        if state == "PENDING":
                            yield "Transcription pending...\n"
                        elif state == "STARTED":
                            yield "Transcription started...\n"
                        elif state == "PROGRESS":
                            yield "Transcription in progress...\n"
                        last = state
                    time.sleep(2)

                if task.successful():
                    yield "Transcription completed.\n\n"
                    transcript = task.get()
                    for line in transcript.split("\n"):
                        if line.strip():
                            yield line.strip() + "\n"
                else:
                    yield "Failed to process the transcript.\n"

            else:
                async def get_text():
                    vid = extract_youtube_video_id(url)
                    return await (fetch_youtube_transcript(vid) if vid else fetch_transcript_for_url(url))

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    yield "Transcription started...\n"
                    text = loop.run_until_complete(get_text())
                    yield "Transcription completed.\n\n"
                    for line in text.split("\n"):
                        if line.strip():
                            yield line.strip() + "\n"
                except Exception:
                    yield "Failed to process the transcript.\n"
                finally:
                    loop.close()

        finally:
            end_time = datetime.datetime.now()
            dur = end_time - start_time
            yield f"\n[PID={pid} REQ={req_id} Duration={dur}]\n"

    return Response(stream_with_context(sse_generate()), mimetype="text/plain")

@api_bp.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200
