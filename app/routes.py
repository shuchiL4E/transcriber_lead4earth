# app/route.py
import asyncio
import os
import uuid
import time
import datetime
import logging
from flask import Blueprint, request, jsonify, Response, render_template, stream_with_context
from .scraper import fetch_transcript_for_url, fetch_youtube_transcript
from .utils import extract_youtube_video_id
from .db import transcripts_collection
from celery_worker import whisper_fallback_task, celery_app, cancel_task

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

@api_bp.route("/savetranscript", methods=["POST"])
def save_transcript():
    if request.is_json:
        url = (request.json.get("url") or "").strip()
    else:
        url = (request.form.get("url") or "").strip()
        
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    return process_transcript(url, save=True)


@api_bp.route("/transcript", methods=["POST"])
def transcript():
    if request.is_json:
        url = (request.json.get("url") or "").strip()
    else:
        url = (request.form.get("url") or "").strip()
        
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    return process_transcript(url, save=False)


def process_transcript(url, save):
    def sse_generate():
        start_time = datetime.datetime.now()
        pid = os.getpid()
        req_id = str(uuid.uuid4())[:8]

        try:
            # 🔹 Case 1: CVTV (always go to Celery Whisper)
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
                    if save:
                        transcripts_collection.insert_one({
                            "url": url,
                            "transcript": transcript
                        })
                    for line in transcript.split("\n"):
                        if line.strip():
                            yield line.strip() + "\n"
                else:
                    yield "Failed to process the transcript.\n"

            # 🔹 Case 2: YouTube or generic
            else:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    vid = extract_youtube_video_id(url)
                    yield "Transcription started...\n"

                    if vid:
                        # ---- YouTube branch ----
                        text = loop.run_until_complete(fetch_youtube_transcript(vid))
                        if save:
                            transcripts_collection.insert_one({"url": url, "transcript": text})
                        yield "Transcription completed.\n\n"
                        for line in text.split("\n"):
                            if line.strip():
                                yield line.strip() + "\n"

                    else:
                        # ---- Generic URL branch ----
                        result = loop.run_until_complete(fetch_transcript_for_url(url))

                        # If scraper signals fallback, delegate to Celery
                        if isinstance(result, dict) and result.get("fallback"):
                            yield "Fallback triggered: running Whisper via Celery…\n"
                            task = whisper_fallback_task.delay(url)
                            while not task.ready():
                                time.sleep(2)
                            if task.successful():
                                transcript = task.get()
                                if save:
                                    transcripts_collection.insert_one({"url": url, "transcript": transcript})
                                yield "Transcription completed.\n\n"
                                yield transcript + "\n"
                            else:
                                yield "Failed to process the transcript.\n"
                        else:
                            # Got a normal transcript
                            if save:
                                transcripts_collection.insert_one({"url": url, "transcript": result})
                            yield "Transcription completed.\n\n"
                            for line in result.split("\n"):
                                if line.strip():
                                    yield line.strip() + "\n"

                except Exception as e:
                    yield f"Failed to process the transcript. Error: {e}\n"
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

