# app/scraper.py
import asyncio
from playwright.async_api import Page, async_playwright
from .utils import parse_vtt
import os
import httpx
from urllib.parse import urljoin
import re 
import uuid
import logging
import requests
import subprocess
import whisper
import math
import wave
import time
import subprocess
import asyncio
from faster_whisper import WhisperModel

#NEW
CABLECAST_H2_MAX_CONN = int(os.getenv("CABLECAST_H2_MAX_CONN", "48"))
CABLECAST_H2_MAX_KEEPALIVE = int(os.getenv("CABLECAST_H2_MAX_KEEPALIVE", "48"))
CABLECAST_FETCH_TIMEOUT = float(os.getenv("CABLECAST_FETCH_TIMEOUT", "6.0"))  # per-request
CABLECAST_RETRIES = int(os.getenv("CABLECAST_RETRIES", "3"))
#NEW

# Configure logging
logging.basicConfig(
    filename="scraper.log",       # log file name
    filemode="a",                 # append mode
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO            # minimum level to log
)
logging.captureWarnings(True)

# scraper.py

async def fallback_to_whisper_html(url: str, whisper_model="tiny",status_cb=None,mp4_url=None):
    """
    FLOW
    1. If captions .m3u8 → stitch VTT.
    2. If MP3 exists → download + transcribe.
    3. If only video HLS (.m3u8) → use ffmpeg:
       (a) fetch audio stream quickly with -c:a copy (.aac),
       (b) convert to WAV,
       (c) transcribe with Whisper.
    4. when nothing happens.. it relaunches the browswer with header and fetch mp4 if exist,
        download it and transcribe it. 
    """
   
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=120000)
            html = await page.content()
            await browser.close()


        


        # 1) Captions via .m3u8
        m3u8_match = re.search(r"https?://[^\s\"']+\.m3u8(?:\?[^\s\"']+)?", html)

        logging.info(".m3u8 found in HTML scanning.......")
        if m3u8_match:
            playlist_url = m3u8_match.group(0)
            try:
                async with httpx.AsyncClient() as client:
                    pl = await client.get(playlist_url)
                    pl.raise_for_status()
                    if ".vtt" in pl.text:
                        stitched_vtt_text = await _stitch_vtt_from_m3u8(playlist_url)
                        return parse_vtt(stitched_vtt_text)
            except Exception as e:
                print(f"[Fallback] Failed m3u8 captions fetch: {e}")
                logging.error(f"[Fallback] Failed m3u8 captions fetch: {e}", exc_info=True)

            
        # 2) Direct MP3
        mp3_match = re.search(r"https?://[^\s\"']+\.mp3", html)
        logging.info(".mp3 found in HTML scanning.......")

        if mp3_match:
            mp3_url = mp3_match.group(0)
            try:
                if status_cb:
                    status_cb("whisper_start", url)
                result = await download_and_transcribe(mp3_url, whisper_model)
                return result
            finally:
                if status_cb:
                    status_cb("whisper_done", url)



        # 3) Video-only HLS → optimized ffmpeg pipeline
        if m3u8_match:
            logging.info(".m3u8.. trying to fetch HLS.......")

            start = time.time()
            playlist_url = m3u8_match.group(0)
            logging.info(f"Extracted playlist_url: {playlist_url}")
            print(f"Extracted playlist_url: {playlist_url}")

            # --- Robust handling ---
            urls_to_try = [playlist_url]
            if playlist_url.endswith("playlist.m3u8") and "?" not in playlist_url:
                urls_to_try.append(playlist_url.replace("playlist.m3u8", "chunklist.m3u8"))

            uid = str(uuid.uuid4())
            aac_file = f"audio_{uid}.aac"
            wav_file = f"audio_{uid}.wav"

            last_err = None
            for candidate_url in urls_to_try:
                try:
                    t0 = time.time()
                    print(f"[HLS] Trying {candidate_url} → {aac_file}")
                    logging.info(f"[HLS] Trying {candidate_url} → {aac_file}")

                    subprocess.run([
                        "ffmpeg", "-y",
                        "-headers", f"Referer: {url}",
                        "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "-i", candidate_url,
                        "-vn", "-c:a", "copy",  # no re-encode
                        aac_file
                    ], check=True)
                    t1 = time.time()
                    print(f"[Timing] Fetch completed in {t1 - t0:.2f} sec")
                    logging.info(f"[Timing] Fetch completed in {t1 - t0:.2f} sec")

                    # Step B: Convert AAC → WAV (quick)
                    print(f"[HLS] Converting AAC → WAV ({wav_file})")
                    logging.info(f"[HLS] Converting AAC → WAV ({wav_file})")

                    subprocess.run([
                        "ffmpeg", "-y",
                        "-i", aac_file,
                        "-ar", "16000", "-ac", "1", "-f", "wav",
                        wav_file
                    ], check=True)
                    t2 = time.time()
                    print(f"[Timing] Conversion completed in {t2 - t1:.2f} sec")
                    logging.info(f"[Timing] Conversion completed in {t2 - t1:.2f} sec")

                    # Step C: Run Whisper
                    if status_cb:
                        status_cb("whisper_start", url)
                    transcript = transcribe_audio(wav_file, whisper_model)

                    if status_cb:
                        status_cb("whisper_done", url)

                    t3 = time.time()
                    print(f"[Timing] Whisper transcription took {t3 - t2:.2f} sec")
                    logging.info(f"[Timing] Whisper transcription took {t3 - t2:.2f} sec")

                    print(f"[Timing] TOTAL elapsed {t3 - t0:.2f} sec")
                    logging.info(f"[Timing] TOTAL elapsed {t3 - t0:.2f} sec")

                    return transcript  # ✅ success

                except Exception as e:
                    last_err = e
                    logging.warning(f"[HLS] Candidate {candidate_url} failed: {e}")
                    continue

                finally:
                    for f in [aac_file, wav_file]:
                        if os.path.exists(f):
                            os.remove(f)

            return {"error": f"HLS fallback failed. Last error: {last_err}"}

    except Exception as e:
        logging.error(f"[Lightweight fallback failed] {e}", exc_info=True)

    # 🚨 Heavy Browser Fallback — Capture MP4 dynamically
        
       
    try:
        logging.info(" Heavy fallback browser initiated...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--enable-features=NetworkService,NetworkServiceInProcess"
                ]
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            # capture either direct VTT TEXT or a captions.m3u8 URL
            loop = asyncio.get_event_loop()
            captions_future: asyncio.Future = loop.create_future()

            async def handle_response(response):
                #print("[DEBUG] Response URL:", response.url)

                if captions_future.done():
                    return
                try:
                    resp_url = response.url or ""
                    status = response.status
                    #print(f"status: {resp_url} ({status})")
                except Exception:
                    return
            
                if resp_url.endswith(".mp4"):
                    print("🎬 .mp4 captured in HTTP response:", resp_url)
                    logging.info(f".mp4 captured: {resp_url}")
                    if not captions_future.done():
                        captions_future.set_result(("mp4", resp_url))
                    return
                
                # Redirect-based .mp4 via Location header
                # this is added for https://townofboone.viebit.com/watch?hash=KAlN7UfIHwy6SPI6 url
            
                if "vod-download" in resp_url:
                    try:
                        headers = await response.all_headers()
                        location = headers.get("location") or headers.get("Location")
                        if location and location.lower().endswith(".mp4"):
                            print(f"🎯 Found Location header pointing to MP4: {location}")
                            if not captions_future.done():
                                captions_future.set_result(("mp4", location))
                            return
                        else:
                            print(f"[DEBUG] vod-download headers: {headers}")
                    except Exception as e:
                        print(f"[WARN] Failed to read headers for vod-download: {e}")

        
            await page.goto(url, wait_until="load", timeout=150000)
            page.on("response", handle_response)
            if "viebit.com" in url:
                    await handle_viebit_url(page)
            
            kind, payload = await asyncio.wait_for(captions_future, timeout=45)
            mp4_url = None
            if kind == "mp4":
                    print("🎯 MP4 captured.........")
                    mp4_url = payload
                    
            return handle_mp4(url,mp4_url,whisper_model="tiny", status_cb=None)

    except Exception as e:
        logging.error(f"Heavy fallback failed: {e}", exc_info=True)

    # 🚫 If nothing worked at all
    return {"error": "No captions (.vtt/.m3u8) or audio/video found"}



def handle_mp4(url: str, mp4_url: str, whisper_model="tiny", status_cb=None):
    """
    Handles remote MP4 → AAC → WAV → Whisper transcription.

    Steps:
      1. Extract audio directly from remote MP4 using ffmpeg (-c:a copy)
      2. Convert AAC → WAV (16kHz mono)
      3. Run Whisper transcription via transcribe_audio()
      4. Clean up temp files and return transcript
    """
    logging.info(f"🎬 [handle_mp4] Starting MP4 → Whisper pipeline for: {mp4_url}")
    uid = str(uuid.uuid4())
    aac_file = f"audio_{uid}.aac"
    wav_file = f"audio_{uid}.wav"

    try:
        t0 = time.time()
        print(f"⚡ [MP4] Extracting audio from remote MP4: {mp4_url}")

        # Step A: Extract audio (AAC) directly from remote MP4 (stream)
        subprocess.run([
            "ffmpeg", "-y",
            "-i", mp4_url,
            "-vn", "-c:a", "copy",
            aac_file
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        t1 = time.time()
        logging.info(f"[Timing] MP4 → AAC extraction completed in {t1 - t0:.2f}s")
        print(f"✅ [Timing] MP4 → AAC extraction completed in {t1 - t0:.2f}s")

        # Step B: Convert AAC → WAV (16kHz mono)
        subprocess.run([
            "ffmpeg", "-y",
            "-i", aac_file,
            "-ar", "16000", "-ac", "1", "-f", "wav", wav_file
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        t2 = time.time()
        logging.info(f"[Timing] AAC → WAV conversion completed in {t2 - t1:.2f}s")
        print(f"✅ [Timing] AAC → WAV conversion completed in {t2 - t1:.2f}s")

        # Step C: Transcribe using Whisper
        if status_cb:
            status_cb("whisper_start", url)
        transcript = transcribe_audio(wav_file, whisper_model)
        if status_cb:
            status_cb("whisper_done", url)

        t3 = time.time()
        logging.info(f"[Timing] Whisper transcription completed in {t3 - t2:.2f}s")
        print(f"✅ [Timing] Whisper transcription completed in {t3 - t2:.2f}s")

        total = t3 - t0
        print(f"🚀 [Total Timing] MP4 → Transcript pipeline finished in {total/60:.1f} min")
        logging.info(f"[Total Timing] MP4 pipeline finished in {total:.2f}s")

        return transcript

    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode(errors="ignore") if e.stderr else str(e)
        logging.error(f"[handle_mp4] FFmpeg error: {err_msg}", exc_info=True)
        return {"error": f"FFmpeg failed: {err_msg}"}

    except Exception as e:
        logging.error(f"[handle_mp4] Unexpected error: {e}", exc_info=True)
        return {"error": f"MP4 handling failed: {e}"}

    finally:
        # Cleanup temporary files
        for f in [aac_file, wav_file]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                    logging.info(f"[Cleanup] Deleted {f}")
                except Exception as e:
                    logging.warning(f"[Cleanup] Failed to delete {f}: {e}")

async def download_and_transcribe(mp3_url: str, whisper_model="tiny"):
    """
    Download an MP3 from a URL, save it temporarily, and run Whisper transcription.
    Cleans up the file afterward.
    """
    uid = str(uuid.uuid4())
    audio_file = f"audio_{uid}.mp3"

    print(f"[Download] Fetching MP3 from {mp3_url}")
    logging.info(f"[Download] Fetching MP3 from {mp3_url}")

    try:
        resp = requests.get(mp3_url, stream=True, timeout=60)
        resp.raise_for_status()
        with open(audio_file, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        print(f"[Download] Saved MP3 → {audio_file}")
        logging.info(f"[Download] Saved MP3 → {audio_file}")


        # Run Whisper transcription
        transcript = stream_whisper_transcription(audio_file, whisper_model=whisper_model)
        return transcript

    except Exception as e:
        logging.error(f"[Download] Failed: {e}", exc_info=True)
        print(f"[Download] Failed: {e}")
        return {"error": str(e)}

    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)
            print(f"[Cleanup] Deleted {audio_file}")
            logging.info(f"[Cleanup] Deleted {audio_file}")


def transcribe_audio(wav_path: str, whisper_model="tiny"):
    model = whisper.load_model(whisper_model)
    print("whisper model loaded......")
    logging.info("whisper model loaded.")

    result = model.transcribe(wav_path, word_timestamps=False, verbose=False)
    print("whisper transcribing completed.....")
    logging.info("whispertranscribing completed..")

    transcript = []
    for seg in result["segments"]:
        if seg.get("text"):
            transcript.append(seg["text"].strip())

    if os.path.exists(wav_path):
        os.remove(wav_path)

    return "\n".join(transcript)

async def stream_whisper_transcription(file_path: str, whisper_model="tiny"):
    """
    Convert MP3 → WAV and stream Whisper transcription line by line.
    """
    print("stream_whisper_transcription caleled........")
    logging.info("stream_whisper_transcription caleled..")

    # Convert MP3 to WAV
    wav_path = os.path.splitext(file_path)[0] + ".wav"
    subprocess.run([
        "ffmpeg", "-y", "-i", file_path,
        "-ar", "16000", "-ac", "1", wav_path
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    print("converted from mp3 to wav.")
    logging.info("converted from mp3 to wav.")

    model = whisper.load_model(whisper_model)

    print("whisper model loaded..")
    logging.info("whisper model loaded.")

    # Use generator-style output
    result = model.transcribe(wav_path, word_timestamps=False, verbose=False)
    print("whisper transcribing completed..")
    logging.info("whisper transcribing completed..")

    try:
        for seg in result["segments"]:
            if seg.get("text"):
                yield seg["text"].strip()
    finally:
        # Clean up files
        if os.path.exists(wav_path):
            os.remove(wav_path)
            print(f"Deleted WAV: {wav_path}")
            logging.info(f"Deleted WAV: {wav_path}")

        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Deleted MP3: {file_path}")
            logging.info(f"Deleted MP3: {file_path}")
            

async def process_cvtv_stream(url: str, whisper_model="tiny",status_cb=None):
    """
    CVTV pipeline that yields Whisper transcript lines as they are ready.
    """
    print("cvtv...........")
    mp3_url = await get_mp3_url(url)
    if not mp3_url:
        yield "[Error] Failed to capture MP3 stream"
        return

    uid = str(uuid.uuid4())
    audio_file = f"audio_{uid}.mp3"

    resp = requests.get(mp3_url, stream=True)
    with open(audio_file, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)

    print("disk written with this audio file.....")
    if status_cb:
        status_cb("whisper_start")

    try:
        async for line in stream_whisper_transcription_openai(audio_file, whisper_model=whisper_model):
        #async for line in stream_faster_whisper_transcription(audio_file, whisper_model=whisper_model):
            yield line
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)
        if status_cb:
            status_cb("whisper_done")

async def stream_whisper_transcription_openai(file_path: str, whisper_model="tiny"):
    """
    Convert MP3 → WAV and stream Whisper transcription line by line.
    """
    # Convert MP3 to WAV
    wav_path = os.path.splitext(file_path)[0] + ".wav"
    subprocess.run([
        "ffmpeg", "-y", "-i", file_path,
        "-ar", "16000", "-ac", "1", wav_path
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    model = whisper.load_model(whisper_model)

    # Use generator-style output
    result = model.transcribe(
        wav_path, 
        word_timestamps=False, 
        verbose=False
        )

    for seg in result["segments"]:
        if seg.get("text"):
            yield seg["text"].strip()

    if os.path.exists(wav_path):
        os.remove(wav_path)


async def stream_faster_whisper_transcription(file_path: str, whisper_model="tiny"):
    """
    Convert MP3 → WAV and stream Faster-Whisper transcription line by line.
    """
    # 1️⃣ Convert MP3 to WAV
    wav_path = os.path.splitext(file_path)[0] + ".wav"
    subprocess.run([
        "ffmpeg", "-y", "-i", file_path,
        "-ar", "16000", "-ac", "1", wav_path
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # 2️⃣ Load Faster-Whisper model (optimized for CPU)
    model = WhisperModel(whisper_model, device="cpu", compute_type="int8")

    # 3️⃣ Transcribe (generator-style streaming)
    segments, info = model.transcribe(
        wav_path,
        beam_size=5,
        vad_filter=True,      # optional: removes long silences
        word_timestamps=False # faster & lighter
    )

    # 4️⃣ Stream each segment as it’s produced
    for segment in segments:
        if segment.text.strip():
            yield segment.text.strip()

    # 5️⃣ Clean up
    if os.path.exists(wav_path):
        os.remove(wav_path)


async def fetch_youtube_transcript(video_id: str):
    """
    Calls the RapidAPI service to get a transcript for a YouTube video.
    """
    api_key = os.getenv("RAPIDAPI_KEY")
    if not api_key:
        raise ValueError("RAPIDAPI_KEY environment variable not set.")

    api_url = f"https://youtube-captions.p.rapidapi.com/transcript?videoId={video_id}"
    headers = {
        "x-rapidapi-host": "youtube-captions.p.rapidapi.com",
        "x-rapidapi-key": api_key,
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(api_url, headers=headers, timeout=30.0)
        # Raise an exception for bad status codes (4xx or 5xx)
        response.raise_for_status() 
        
        data = response.json()

        # Assuming the API returns a list of caption segments, each with a 'text' key.
        # We join them together to form the full transcript.
        if not isinstance(data, list):
            raise TypeError("Expected a list of captions from the YouTube API.")
        
        transcript_lines = [item.get("text", "") for item in data]
        return "\n".join(transcript_lines)


async def handle_granicus_url(page: 'Page'):
    """Performs the UI trigger sequence for Granicus (Dublin) pages."""
    print("  - Detected Granicus platform. Executing trigger sequence...")
#    await page.screenshot(path='/app/screenshots/before_click.png')
    await page.locator(".flowplayer").hover(timeout=2000)


    element = page.locator(".fp-menu").get_by_text("On", exact=True)
    await element.scroll_into_view_if_needed(timeout=2000)
    await element.click(force=True)
#    await page.screenshot(path='/app/screenshots/after_click.png')

async def handle_viebit_url(page: 'Page'):
    """Performs the UI trigger sequence for Viebit (Fremont) pages."""
    print("  - Detected Viebit platform. Executing trigger sequence...")

    # ✅ Combine all initial UI actions under one try-except block
    try:
        await page.locator(".vjs-big-play-button").click(timeout=2000)
        print("  - Clicked big play button.")
        await page.locator(".vjs-play-control").click(timeout=2000)
        print("  - Clicked play control.")
        await page.wait_for_timeout(500)

        # Captions 
        await page.locator("button.vjs-subs-caps-button").click(timeout=2000)
        print("  - Clicked captions button.")
        await page.locator('.vjs-menu-item:has-text("English")').click(timeout=2000)
        print("  - Enabled English captions.")

    except Exception as e:
        print(f"  - [WARN] One or more UI triggers failed: {e}")

    # Continue to look for download button: 
    # this is to handle: https://townofboone.viebit.com/
    try:
        print("  - Looking for download button...")
        await page.wait_for_selector("button.btn.btn-default.btn-xs.btnVodDownload", timeout=5000)
        await page.click("button.btn.btn-default.btn-xs.btnVodDownload")
        print("  - Clicked Download button. Waiting for request...")
        await page.wait_for_timeout(7000)
    except Exception as e:
        print(f"  - [WARN] Download button not found or click failed: {e}")


async def handle_cablecast_url(page: 'Page'):
    """UI trigger for Cablecast (video.js) players."""
    print("  - Detected Cablecast platform. Executing trigger sequence...")
    # Try the big play button, then fallback to the toolbar play control.
    try:
        await page.locator(".vjs-big-play-button").click(timeout=8000)
    except Exception:
        try:
            await page.locator(".vjs-play-control").click(timeout=8000)
        except Exception:
            pass

    # Give the player a moment to initialize HLS/captions
    await page.wait_for_timeout(500)

    # Try to open CC menu and enable the first available track (English if present).
    try:
        await page.locator(".vjs-subs-caps-button, .vjs-captions-button").click(timeout=8000)
        # Prefer “English”, else just the first unchecked item.
        english = page.locator(".vjs-menu-item:has-text('English')")
        if await english.count() > 0:
            await english.first.click(timeout=8000)
        else:
            unchecked = page.locator(".vjs-menu-item[aria-checked='false']")
            if await unchecked.count() > 0:
                await unchecked.first.click(timeout=8000)
    except Exception:
        # Some streams auto-enable captions or expose them as default tracks.
        pass

# NEW

async def _stitch_vtt_from_m3u8(playlist_url: str) -> str:
    """
    Download a captions .m3u8 and stitch all .vtt segments into a single VTT text.
    Optimized for Cablecast: HTTP/2, pooled connections, bounded concurrency, retries.
    Returns the stitched VTT TEXT (not file).
    """
    limits = httpx.Limits(
        max_connections=CABLECAST_H2_MAX_CONN,
        max_keepalive_connections=CABLECAST_H2_MAX_KEEPALIVE,
    )
    timeout = httpx.Timeout(CABLECAST_FETCH_TIMEOUT)

    async with httpx.AsyncClient(http2=True, limits=limits, timeout=timeout) as client:
        # 1) Fetch playlist
        pl = await client.get(playlist_url)
        pl.raise_for_status()
        base = playlist_url.rsplit("/", 1)[0] + "/"

        # 2) Build absolute segment URLs (ignore #EXT* comment lines)
        seg_urls = []
        for line in pl.text.splitlines():
            ln = line.strip()
            if ln and not ln.startswith("#"):
                seg_urls.append(urljoin(base, ln))

        if not seg_urls:
            raise RuntimeError("Captions playlist contained no segments")

        # 3) Fetch segments with bounded concurrency + retries
        sem = asyncio.Semaphore(CABLECAST_H2_MAX_CONN)

        async def fetch_seg(idx: int, seg_url: str) -> tuple[int, str]:
            attempt = 0
            while True:
                attempt += 1
                try:
                    async with sem:
                        r = await client.get(seg_url)
                    if r.status_code != 200 or not r.text.strip():
                        raise httpx.HTTPError(f"bad status {r.status_code}")
                    return idx, r.text
                except Exception:
                    if attempt > CABLECAST_RETRIES:
                        return idx, ""
                    await asyncio.sleep(0.05 * attempt)

        tasks = [fetch_seg(i, u) for i, u in enumerate(seg_urls)]
        results = await asyncio.gather(*tasks)

    # 4) Reassemble in-order; skip empties
    results.sort(key=lambda t: t[0])
    chunks = [txt for _, txt in results if txt]

    if not chunks:
        raise RuntimeError("No caption segments could be downloaded")

    # 5) Prepend header and join
    stitched_vtt = "WEBVTT\n\n" + "\n\n".join(chunks) + "\n"
    return stitched_vtt

async def get_mp3_url(url: str):
    """
    Use Playwright to scrape the Audio download link from CVTV DOM.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")

        try:
            mp3_href = await page.locator("a[href$='.mp3']").get_attribute("href")
            if mp3_href:
                print(f"[CVTV] Found MP3 link in DOM: {mp3_href}")
                return mp3_href
        except Exception as e:
            print(f"[CVTV] Failed to extract MP3 link: {e}")
        finally:
            await browser.close()

    return None



async def fetch_transcript_for_url(url: str):
    print("fetch transcript for url...................")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        # capture either direct VTT TEXT or a captions.m3u8 URL
        loop = asyncio.get_event_loop()
        captions_future: asyncio.Future = loop.create_future()

        async def handle_response(response):
            print("[DEBUG] Response URL:", response.url)

            if captions_future.done():
                return
            try:
                resp_url = response.url or ""
                resp_url_lower = resp_url.lower()
                status = response.status
                #print(f"status: {resp_url} ({status})")
            except Exception:
                return

            # Prefer captions playlists (we'll stitch segments later)
            if resp_url_lower.endswith(".m3u8") and "captions" in resp_url_lower:
                print(".m3u8 captutured in HTTP response")
                if not captions_future.done():
                    captions_future.set_result(("m3u8", response.url))
                return

            # Otherwise accept any .vtt file content directly
            if ".vtt" in resp_url_lower:
                print(".vtt captutured in HTTP response")
                try:
                    vtt_text = await response.text()
                    if not captions_future.done():
                        captions_future.set_result(("vtt", vtt_text))
                except Exception as e:
                    if not captions_future.done():
                        captions_future.set_exception(e)

            if resp_url_lower.endswith(".srt"):
                print(".srt captured in HTTP response")
                try:
                    srt_text = await response.text()
                    if not captions_future.done():
                        captions_future.set_result(("srt", srt_text))
                except Exception as e:
                    if not captions_future.done():
                        captions_future.set_exception(e)


            # 🎯 Audio file (.mp3)
            if resp_url_lower.endswith(".mp3"):
                print(".mp3 captutured in HTTP response")
                logging.info(".mp3 captutured in HTTP response")
                if not captions_future.done():
                    captions_future.set_result(("mp3", response.url))
                return
            
        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="load", timeout=0)

            # strict platform routing (no generic fallback)
            if "granicus.com" in url:
                try:
                    await handle_granicus_url(page)
                except Exception as e:
                    print(f"[Granicus] trigger skipped: {e}")

            elif "viebit.com" in url:
                try:
                    await handle_viebit_url(page)
                except Exception as e:
                    print(f"[viebit] trigger skipped: {e}")

            elif ".cablecast.tv" in url or ".concord" in url:
                await handle_cablecast_url(page)
            elif ".cvtv.org" in url:
                return await process_cvtv_stream(url)
            else:
                #await fetch_texttracks(url)
                print("[Info] Unknown platform detected. Proceeding with generic sniffing...")
                # Wait for some seconds to allow video and captions to load
                await page.wait_for_timeout(8000)
                #raise ValueError("Unknown platform. Could not process URL.")

            try:
                # wait for either a .vtt payload or a captions.*.m3u8 URL
                kind, payload = await asyncio.wait_for(captions_future, timeout=45)

                if kind == "vtt":
                    print("vtt is called")
                    logging.info("vtt is called")
                    return parse_vtt(payload)

                if kind == "m3u8":
                    print("m3u8 is called")
                    logging.info("m3u8 is called")
                    stitched_vtt_text = await _stitch_vtt_from_m3u8(payload)
                    return parse_vtt(stitched_vtt_text)

                if kind == "srt":
                    print("srt is called")
                    logging.info("srt is called")
                    return parse_vtt(payload)

            except Exception as e:
                print(f"[Captions] Network sniffing failed or timed out: {e}")
                logging.error(f"[Captions] Network sniffing failed or timed out: {e}", exc_info=True)
                logging.info("delegating process to celery.....")
                return {"fallback": True, "url": url}
                #return await fallback_to_whisper_html(url, whisper_model="tiny")

            raise ValueError("No captions found via network or fallback.")

        finally:
            await browser.close()

