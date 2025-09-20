import asyncio
import os
import uuid
from flask import Blueprint, request, jsonify, Response, render_template, stream_with_context
import requests
import datetime


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
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")

    def generate():
        start_time = datetime.datetime.now()
        header = f"[{start_time}] PID={os.getpid()} START {url}\n"


        # 🔹 print to console/log
        print(header, flush=True)

        # 🔹 also append to requests.log
        with open("requests.log", "a") as f:
            f.write(header)

        yield header  # so it also shows up in outputN.txt

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
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
                        line = loop.run_until_complete(agen.__anext__())
                        yield line
                except StopAsyncIteration:
                    pass
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
        finally:
            end_time = datetime.datetime.now()
            duration = end_time - start_time
            footer = f"[{end_time}] PID={os.getpid()} END {url} (duration: {duration})\n"

            # 🔹 print to console/log
            print(footer, flush=True)

            # 🔹 also append to requests.log
            with open("requests.log", "a") as f:
                f.write(footer)

            yield footer  # include in outputN.txt

    return Response(stream_with_context(generate()), mimetype="text/plain")



@api_bp.route("/health", methods=["GET"])
def health_check():
    """Simple health check endpoint."""
    return jsonify({"status": "healthy"}), 200
