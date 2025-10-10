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
    #transcript_text = process_transcript(url)
    #return jsonify({"transcript": transcript_text})
    return process_transcript(url, save=False)



def process_transcript(url, save):
    """
    Unified transcript processor (non-streaming).
    - If it's a CVTV URL → immediately enqueue Celery (Whisper fallback)
    - If it's any other URL:
        1. Try direct extraction (YouTube / VTT / etc.)
        2. If fetch_transcript_for_url() returns {"fallback": True} → enqueue Celery
        3. Otherwise return full transcript as JSON
    """

    # Case 1: CVTV → always fallback to Celery
    if ".cvtv.org" in url:
        task = whisper_fallback_task.delay(url)
        transcripts_collection.insert_one({
            "url": url,
            "task_id": task.id,
            "status": "IN_PROGRESS",
            "transcript": None,
            "created_at": datetime.datetime.utcnow(),
        })
        return jsonify({
            "task_id": task.id,
            "status": "IN_PROGRESS",
            "message": (
                "🧠 Whisper fallback started.\n"
                f"📘 Token ID: {task.id}\n\n"
                "⏳ Please copy this Token ID and check again after 15–20 minutes using the form below."
            )
        }), 202

    # Case 2: Other URLs → try direct extraction, fallback if needed
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        vid = extract_youtube_video_id(url)
        if vid:
            # YouTube transcripts
            text = loop.run_until_complete(fetch_youtube_transcript(vid))
            if save:
                transcripts_collection.insert_one({
                    "url": url,
                    "transcript": text,
                    "status": "COMPLETED",
                    "created_at": datetime.datetime.utcnow(),
                })
            return jsonify({
                "status": "COMPLETED",
                "source": "youtube",
                "transcript": text,
                "url": url,
                "created_at": datetime.datetime.utcnow(),
            }), 200

        # Non-YouTube: handle custom sources
        result = loop.run_until_complete(fetch_transcript_for_url(url))

        # ⚡ If fallback required (e.g., no captions found)
        if isinstance(result, dict) and result.get("fallback"):
            task = whisper_fallback_task.delay(url)
            transcripts_collection.insert_one({
                "url": url,
                "task_id": task.id,
                "status": "IN_PROGRESS",
                "transcript": None,
                "created_at": datetime.datetime.utcnow(),
            })
            return jsonify({
                "task_id": task.id,
                "status": "IN_PROGRESS",
                "message": (
                    "🧠 Whisper fallback started.\n"
                    f"📘 Token ID: {task.id}\n\n"
                    "⏳ Please copy this Token ID and check again after 15–20 minutes using the form below."
                )
            }), 202

        # ✅ Normal transcript success (no fallback)
        if save:
            transcripts_collection.insert_one({
                "url": url,
                "transcript": result,
                "status": "COMPLETED",
                "created_at": datetime.datetime.utcnow(),
            })
        return jsonify({
            "status": "COMPLETED",
            "source": "direct",
            "transcript": result,
            "url": url,
            "created_at": datetime.datetime.utcnow(),
        }), 200

    except Exception as e:
        # Error handling
        logging.error(f"Error processing transcript for {url}: {e}")
        return jsonify({
            "status": "FAILED",
            "error": str(e),
            "url": url
        }), 500

    finally:
        loop.close()


def process_transcript_streaming_response(url, save):
    """
    Hybrid:
    - Fast SSE stream for direct transcripts
    - JSON response (task_id) for Whisper fallback
    """
    # Case 1: CVTV → Always fallback to Celery
    if ".cvtv.org" in url:
        task = whisper_fallback_task.delay(url)

        transcripts_collection.insert_one({
            "url": url,
            "task_id": task.id,
            "status": "IN_PROGRESS",
            "transcript": None,
            "created_at": datetime.datetime.utcnow(),
        })

        result = jsonify({
            "task_id": task.id,
            "status": "IN_PROGRESS",
            "message": (
                "🧠 Whisper fallback started.\n"
                "📘 Task ID: " + task.id + "\n\n"
                "⏳ Please copy this Task ID and check again after 15–20 minutes using the form below."
            )
        }), 202

        
        return result

    # Case 2: Others (possibly fallback after scrape)
    def sse_generate():
        start_time = datetime.datetime.now()
        #pid = os.getpid()
        #req_id = str(uuid.uuid4())[:8]

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            vid = extract_youtube_video_id(url)
            yield "🚀 Transcription started...\n"

            if vid:
                text = loop.run_until_complete(fetch_youtube_transcript(vid))
                if save:
                    transcripts_collection.insert_one({"url": url, "transcript": text})
                yield "✅ Transcription completed.\n\n"
                for line in text.split("\n"):
                    yield line.strip() + "\n"

            else:
                result = loop.run_until_complete(fetch_transcript_for_url(url))

                # ⚡️ Fallback triggered → stop streaming, enqueue Celery
                if isinstance(result, dict) and result.get("fallback"):
                    task = whisper_fallback_task.delay(url)
                    transcripts_collection.insert_one({
                        "url": url,
                        "task_id": task.id,
                        "status": "IN_PROGRESS",
                        "transcript": None,
                        "created_at": datetime.datetime.utcnow(),
                    })
                    # ❗ Immediately return JSON instead of streaming
                    loop.close()
                    return jsonify({
                        "task_id": task.id,
                        "status": "IN_PROGRESS",
                        "message": (
                            "🧠 Whisper fallback started.\n"
                            f"📘 Task ID: {task.id}\n\n"
                            "⏳ Please copy this Task ID and check again after 15–20 minutes using the form below."
                        )
                    }), 202

                # ✅ Normal success
            
                if save:
                    transcripts_collection.insert_one({"url": url, "transcript": result})
                yield "✅ Transcription completed.\n\n"
                for line in result.split("\n"):
                    yield line.strip() + "\n"

        except StopIteration as e:
            # Break out and return JSON
            return e.value

        finally:
            loop.close()

    return Response(stream_with_context(sse_generate()), mimetype="text/plain")


@api_bp.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200

@api_bp.route("/status/<task_id>", methods=["GET"])
def get_task_status(task_id):
    """
    Fetch task status and transcript by task_id from MongoDB.
    """
    doc = transcripts_collection.find_one({"task_id": task_id})
    if not doc:
        return jsonify({"error": "Task not found"}), 404

    return jsonify({
        "task_id": task_id,
        "status": doc.get("status", "UNKNOWN"),
        "transcript": doc.get("transcript"),
        "url": doc.get("url"),
        "created_at": doc.get("created_at"),
        "completed_at": doc.get("completed_at"),
    })
