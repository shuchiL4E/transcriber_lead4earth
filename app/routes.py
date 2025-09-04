import asyncio
import os
import uuid
from flask import Blueprint, request, jsonify, Response, render_template, stream_with_context
import requests

from .scraper import (
    fetch_transcript_for_url,
    fetch_youtube_transcript,
    process_cvtv_stream,
)
from .utils import extract_youtube_video_id


api_bp = Blueprint("api", __name__, template_folder="templates")


@api_bp.route("/gettranscript", methods=["GET"])
def get_form():
    """Serve the input form HTML."""
    return render_template("index.html")


@api_bp.route("/transcript", methods=["POST"])
def get_transcript():
    """Stream transcript output line by line."""
    url = request.form.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Normalize URL (prepend https:// if missing)
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")

    def generate():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # --- CVTV: Whisper streaming ---
        if ".cvtv.org" in url:
            async def iterate_stream():
                try:
                    async for line in process_cvtv_stream(url, whisper_model="tiny"):
                        yield line + "\n"
                except Exception as e:
                    yield f"[Error] CVTV processing failed: {e}\n"

            agen = iterate_stream()
            try:
                while True:
                    # bridge async generator -> sync yield
                    line = loop.run_until_complete(agen.__anext__())
                    yield line
            except StopAsyncIteration:
                pass

        # --- Other platforms: YouTube, Granicus, Viebit, Cablecast ---
        else:
            async def get_text():
                vid = extract_youtube_video_id(url)
                return await (
                    fetch_youtube_transcript(vid) if vid else fetch_transcript_for_url(url)
                )

            try:
                text = loop.run_until_complete(get_text())
                for line in text.splitlines():
                    yield line + "\n"
            except Exception as e:
                yield f"[Error] Transcript fetch failed: {e}\n"

    return Response(stream_with_context(generate()), mimetype="text/plain")


@api_bp.route("/health", methods=["GET"])
def health_check():
    """Simple health check endpoint."""
    return jsonify({"status": "healthy"}), 200
