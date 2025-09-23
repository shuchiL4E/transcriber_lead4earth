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
from faster_whisper import WhisperModel
import subprocess
import asyncio
#from faster_whisper import WhisperModel

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

async def fallback_to_whisper_html(url: str, whisper_model="tiny",status_cb=None):
    
    """
    Fallback for Granicus:
    1. If captions .m3u8 → stitch VTT.
    2. If MP3 exists → download + transcribe.
    3. If only video HLS (.m3u8) → use ffmpeg:
       (a) fetch audio stream quickly with -c:a copy (.aac),
       (b) convert to WAV,
       (c) transcribe with Whisper.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=120000)
        html = await page.content()
        await browser.close()

    # 1) Captions via .m3u8
    m3u8_match = re.search(r"https?://[^\s\"']+\.m3u8", html)
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
        start = time.time()
        playlist_url = m3u8_match.group(0)
        if playlist_url.endswith("playlist.m3u8"):
            playlist_url = playlist_url.replace("playlist.m3u8", "chunklist.m3u8")

        uid = str(uuid.uuid4())
        aac_file = f"audio_{uid}.aac"
        wav_file = f"audio_{uid}.wav"

        try:
            # Step A: Fetch audio (fast, no transcoding)
            t0 = time.time()
            print(f"[HLS] Fetching AAC audio → {aac_file}")
            logging.info(f"[HLS] Fetching AAC audio → {aac_file}")

            subprocess.run([
                "ffmpeg", "-y",
                "-i", playlist_url,
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
            # Step C: Whisper
            if status_cb:
                status_cb("whisper_start", url)
            transcript = transcribe_audio(wav_file, whisper_model="tiny")
            
        
            t3 = time.time()
            print(f"[Timing] Whisper transcription took {t3 - t2:.2f} sec")
            logging.info(f"[Timing] Whisper transcription took {t3 - t2:.2f} sec")

            print(f"[Timing] TOTAL elapsed {t3 - t0:.2f} sec")
            logging.info(f"[Timing] TOTAL elapsed {t3 - t0:.2f} sec")

            return transcript

        except Exception as e:
            print(f"[HLS] Fallback failed: {e}")
            #logging.error(f"[HLS] Fallback failed: {e}", exc_info=True)

            return {"error": str(e)}

        finally:
            if status_cb:
                status_cb("whisper_done", url)
            for f in [aac_file, wav_file]:
                if os.path.exists(f):
                    os.remove(f)

    return {"error": "No captions (.vtt/.m3u8) or audio found"}



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


async def transcribe_audio_old(wav_path: str, whisper_model="tiny"):
    
    model = whisper.load_model(whisper_model)

    print("whisper model loaded....................")
    logging.info("whisper model loaded.")

    # Use generator-style output
    result = model.transcribe(wav_path, word_timestamps=False, verbose=False)
    logging.info("whispertranscribing completed..")

    for seg in result["segments"]:
        if seg.get("text"):
            yield seg["text"].strip()

    if os.path.exists(wav_path):
        os.remove(wav_path)


def transcribe_audio(wav_path: str, whisper_model="tiny"):
    model = whisper.load_model(whisper_model)
    print("whisper model loaded....................")
    logging.info("whisper model loaded.")

    result = model.transcribe(wav_path, word_timestamps=False, verbose=False)
    print("whispertranscribing completed....................")
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
            

async def stream_whisper_transcription_old(file_path: str, whisper_model="tiny"):
    """
    Real-time streaming transcription with faster-whisper.
    Yields segments incrementally with progress markers.
    """
    # Load faster-whisper model
    model = WhisperModel(whisper_model, device="cpu", compute_type="int8")

    # Stream transcription (segments is a generator)
    segments, info = model.transcribe(file_path, beam_size=5, word_timestamps=False)

    total_duration = info.duration if info and info.duration else None

    for seg in segments:   # This yields as soon as each segment is ready
        if seg.text:
            progress = 0
            if total_duration:
                progress = int((seg.end / total_duration) * 100)
            yield f"[{progress}%] {seg.text.strip()}"



async def process_cvtv_stream(url: str, whisper_model="tiny",status_cb=None):
    """
    CVTV pipeline that yields Whisper transcript lines as they are ready.
    """
    
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
    await page.locator(".flowplayer").hover(timeout=10000)


    element = page.locator(".fp-menu").get_by_text("On", exact=True)
    await element.scroll_into_view_if_needed(timeout=10000)
    await element.click(force=True)
#    await page.screenshot(path='/app/screenshots/after_click.png')


async def handle_viebit_url(page: 'Page'):
    """Performs the UI trigger sequence for Viebit (Fremont) pages."""
    print("  - Detected Viebit platform. Executing trigger sequence...")
    await page.locator(".vjs-big-play-button").click(timeout=10000)
    await page.locator(".vjs-play-control").click(timeout=10000)
    await page.wait_for_timeout(500)
    await page.locator("button.vjs-subs-caps-button").click(timeout=10000)
    await page.locator('.vjs-menu-item:has-text("English")').click(timeout=10000)

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



async def process_cvtv(url: str):
    """
    Full pipeline for CVTV: scrape MP3 link from DOM → download → Whisper.
    """
    start_time = time.time() 
    mp3_url = await get_mp3_url(url)
    if not mp3_url:
        return {"error": "Failed to capture MP3 stream"}

    uid = str(uuid.uuid4())
    audio_file = f"audio_{uid}.mp3"

    print(f"[CVTV] Downloading MP3: {mp3_url}")
    resp = requests.get(mp3_url, stream=True)
    with open(audio_file, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)

    print(f"Now calling run_whisper_transcription method....")
    #transcript = run_whisper_openai(audio_file, whisper_model="tiny")
    transcript = stream_whisper_transcription(audio_file, whisper_model="tiny")

    # 🔹 Check MP3 size
    if os.path.exists(audio_file):
        size_bytes = os.path.getsize(audio_file)
        size_mb = size_bytes / (1024 * 1024)
        print(f"[CVTV] Downloaded MP3 size: {size_mb:.2f} MB ({size_bytes} bytes)")

    end_time = time.time()
    elapsed = end_time - start_time
    mins, secs = divmod(elapsed, 60)
    print(f"Processing took: {int(mins)} min {secs:.2f} sec")

    if os.path.exists(audio_file):
        os.remove(audio_file)
        print(f"[Cleanup] Deleted {audio_file}")

    return transcript


def run_whisper_openai(file_path, whisper_model="tiny"):
    """
    Convert MP3 → WAV (16kHz mono) and transcribe with Whisper.
    """
    print(f"[Whisper] Starting transcription on {file_path}")

    # Convert MP3 to WAV
    wav_path = os.path.splitext(file_path)[0] + ".wav"
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-i", file_path,
            "-ar", "16000",   # sample rate 16kHz
            "-ac", "1",       # mono
            wav_path
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"[Whisper] Converted to WAV: {wav_path}")
    except subprocess.CalledProcessError as e:
        print(f"[FFmpeg] Failed: {e.stderr.decode()}")
        return {"error": f"FFmpeg failed: {e.stderr.decode()}"}

    """
    Transcribe audio using OpenAI's whisper library.
    """
    try:
        model = whisper.load_model(whisper_model)  # tiny, base, small, medium, large
        result = model.transcribe(wav_path)
        print(result["text"])
        return result["text"]
    except Exception as e:
        print(f"[Whisper] Failed: {e}")
        return {"error": str(e)}
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
            print(f"[Cleanup] Deleted {wav_path}")
    

def run_whisper_transcription(file_path: str, whisper_model="tiny"):
    """
    Convert MP3 → WAV (16kHz mono) and transcribe with Whisper.
    """
    print(f"[Whisper] Starting transcription on {file_path}")

    # Convert MP3 to WAV
    wav_path = os.path.splitext(file_path)[0] + ".wav"
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-i", file_path,
            "-ar", "16000",   # sample rate 16kHz
            "-ac", "1",       # mono
            wav_path
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"[Whisper] Converted to WAV: {wav_path}")
    except subprocess.CalledProcessError as e:
        print(f"[FFmpeg] Failed: {e.stderr.decode()}")
        return {"error": f"FFmpeg failed: {e.stderr.decode()}"}

    # Load Whisper model and transcribe
    try:
        model = WhisperModel("base", device="cpu", compute_type="float32")

        #model = whisper.load_model(whisper_model)
        segments, info = model.transcribe(wav_path,language="en")
        transcript_parts = []
        for i, seg in enumerate(segments):
            print(seg)
            try:
                if seg.text:
                    print(seg.text)
                    #transcript_parts.append(seg.text.strip())
            except Exception as e:
                print(f"[Whisper Warning] Failed at segment {i}: {e}")
                continue
        

        transcript = " ".join(transcript_parts)

        print("[Whisper] Transcription finished successfully.")
        return transcript

        #return {"text": result["text"]}
    except Exception as e:
        print(f"[Whisper] Failed: {e}")
        return {"error": str(e)}
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
            print(f"[Cleanup] Deleted {wav_path}")



async def fetch_transcript_for_url(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        # capture either direct VTT TEXT or a captions.m3u8 URL
        loop = asyncio.get_event_loop()
        captions_future: asyncio.Future = loop.create_future()

        async def handle_response(response):
            if captions_future.done():
                return
            try:
                resp_url = (response.url or "").lower()
            except Exception:
                return

            # Prefer captions playlists (we'll stitch segments later)
            if resp_url.endswith(".m3u8") and "captions" in resp_url:
                print(".m3u8 captutured in HTTP response")
                if not captions_future.done():
                    captions_future.set_result(("m3u8", response.url))
                return

            # Otherwise accept any .vtt file content directly
            if ".vtt" in resp_url:
                print(".vtt captutured in HTTP response")
                try:
                    vtt_text = await response.text()
                    if not captions_future.done():
                        captions_future.set_result(("vtt", vtt_text))
                except Exception as e:
                    if not captions_future.done():
                        captions_future.set_exception(e)

            # 🎯 Audio file (.mp3)
            if resp_url.endswith(".mp3"):
                print(".mp3 captutured in HTTP response")
                logging.info(".mp3 captutured in HTTP response")
                if not captions_future.done():
                    captions_future.set_result(("mp3", response.url))
                return

        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="load", timeout=450000)

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

            elif ".cablecast.tv" in url:
                await handle_cablecast_url(page)
            elif ".cvtv.org" in url:
                return await process_cvtv_stream(url)
            else:
                raise ValueError("Unknown platform. Could not process URL.")

            try:
                # wait for either a .vtt payload or a captions.*.m3u8 URL
                kind, payload = await asyncio.wait_for(captions_future, timeout=15)

                if kind == "vtt":
                    print("vtt is called")
                    logging.info("vtt is called")
                    return parse_vtt(payload)

                if kind == "m3u8":
                    print("m3u8 is called")
                    logging.info("m3u8 is called")
                    stitched_vtt_text = await _stitch_vtt_from_m3u8(payload)
                    return parse_vtt(stitched_vtt_text)

            except Exception as e:
                print(f"[Captions] Network sniffing failed or timed out: {e}")
                logging.error(f"[Captions] Network sniffing failed or timed out: {e}", exc_info=True)
                return {"fallback": True, "url": url}
                #return await fallback_to_whisper_html(url, whisper_model="tiny")

            raise ValueError("No captions found via network or fallback.")

        finally:
            await browser.close()


async def fetch_transcript_for_url_old(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})  # Set a standard viewport size
        page = await context.new_page()
        vtt_future = asyncio.Future()

        async def handle_response(response):
            if ".vtt" in response.url and not vtt_future.done():
                try: vtt_future.set_result(await response.text())
                except Exception as e:
                    if not vtt_future.done(): vtt_future.set_exception(e)
        
        page.on("response", handle_response)
        
        try:
            await page.goto(url, wait_until="load", timeout=450000)
            if "granicus.com" in url:
                await handle_granicus_url(page)
            elif "viebit.com" in url:
                await handle_viebit_url(page)
            else:
                raise ValueError("Unknown platform. Could not process URL.")
            
            vtt_content = await asyncio.wait_for(vtt_future, timeout=20)
            return parse_vtt(vtt_content)
        finally:
            await browser.close()
 