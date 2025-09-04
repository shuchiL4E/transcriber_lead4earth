import asyncio
from playwright.async_api import async_playwright
from .utils import parse_vtt
import os
import httpx
from urllib.parse import urljoin
import uuid
import requests
import subprocess
import whisper
import re
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
# --------------------------
# Config
# --------------------------

CABLECAST_H2_MAX_CONN = int(os.getenv("CABLECAST_H2_MAX_CONN", "48"))
CABLECAST_H2_MAX_KEEPALIVE = int(os.getenv("CABLECAST_H2_MAX_KEEPALIVE", "48"))
CABLECAST_FETCH_TIMEOUT = float(os.getenv("CABLECAST_FETCH_TIMEOUT", "6.0"))
CABLECAST_RETRIES = int(os.getenv("CABLECAST_RETRIES", "3"))

# --------------------------
# Helpers
# --------------------------

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
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            raise TypeError("Expected a list of captions from the YouTube API.")

        transcript_lines = [item.get("text", "") for item in data]
        return "\n".join(transcript_lines)


def download_and_transcribe_audio(url_or_file: str, prefer="mp3", whisper_model="tiny"):
    """
    Fallback: download MP3/MP4 (or accept local file).
    Convert → WAV → Whisper transcription.
    """
    uid = str(uuid.uuid4())
    ext = prefer if prefer in ["mp3", "mp4"] else "mp3"

    # If already a local file
    if os.path.exists(url_or_file):
        local_file = url_or_file
        print(f"[Fallback] Using local file: {local_file}")
    else:
        local_file = f"audio_{uid}.{ext}"
        try:
            print(f"[Fallback] Downloading {ext.upper()} from {url_or_file}")
            resp = requests.get(url_or_file, stream=True, timeout=60)
            resp.raise_for_status()
            with open(local_file, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
        except Exception as e:
            print(f"[Fallback] Failed to download {url_or_file}: {e}")
            return {"error": str(e)}

    try:
        # Convert to WAV
        wav_path = os.path.splitext(local_file)[0] + ".wav"
        subprocess.run([
            "ffmpeg", "-y",
            "-i", local_file,
            "-ar", "16000",
            "-ac", "1",
            wav_path
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"[FFmpeg] Converted to WAV: {wav_path}")

        # Whisper transcription
        print(f"[Whisper] Loading model: {whisper_model}")
        model = whisper.load_model(whisper_model)
        result = model.transcribe(wav_path)

        print(f"[Whisper] Transcription finished")
        return result["text"]

    except Exception as e:
        print(f"[Whisper] Failed: {e}")
        return {"error": str(e)}

    finally:
        for f in [local_file, local_file.replace(f".{ext}", ".wav")]:
            if os.path.exists(f):
                os.remove(f)
                print(f"[Cleanup] Deleted {f}")

def get_hls_url_generic(url: str, wait_time: int = 5) -> str:
    """
    Launch page with Selenium, search all scripts + page HTML for .m3u8 URLs.
    Works for Viebit, Cablecast, Granicus, etc.
    """
    print(f"[HLS] Launching browser for {url}")
    driver = webdriver.Chrome()

    try:
        driver.get(url)
        time.sleep(wait_time)  # let the page fully render

        # Collect candidates from <script> tags
        candidates = []
        scripts = driver.find_elements("tag name", "script")
        for script in scripts:
            text = script.get_attribute("innerHTML")
            if text:
                candidates.append(text)

        # Add full page source as backup
        candidates.append(driver.page_source)

        # Search all text blocks for .m3u8
        for block in candidates:
            matches = re.findall(r"https?://[^\s\"']+\.m3u8[^\s\"']*", block)
            if matches:
                print(f"[HLS] Found m3u8: {matches[0]}")
                return matches[0]

        print("[HLS] No .m3u8 found in HTML or scripts")
        return None

    finally:
        driver.quit()

def get_hls_url_selenium(url: str, wait_time: int = 10) -> str:
    """
    Open page with Selenium, capture network requests, and return first .m3u8 URL.
    """
    print(f"[HLS-Selenium] Launching browser for {url}")
    driver = webdriver.Chrome()

    try:
        driver.get(url)
        time.sleep(wait_time)  # allow video player to load and request streams

        for request in driver.requests:
            if request.response and ".m3u8" in request.url and "captions" not in request.url.lower():
                print(f"[HLS-Selenium] Found stream: {request.url}")
                return request.url

        print("[HLS-Selenium] No .m3u8 found in network traffic")
        return None

    finally:
        driver.quit()

def download_from_m3u8(m3u8_url: str, out_mp4: str) -> str:
    """
    Download an .m3u8 HLS video playlist and save as MP4 using ffmpeg.
    """
    try:
        print(f"[FFmpeg] Downloading video from HLS: {m3u8_url}")
        subprocess.run([
            "ffmpeg", "-y",
            "-i", m3u8_url,
            "-c", "copy",
            out_mp4
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"[FFmpeg] HLS → MP4 saved at {out_mp4}")
        return out_mp4
    except subprocess.CalledProcessError as e:
        print(f"[FFmpeg] Failed to download from m3u8: {e.stderr.decode()}")
        return None


async def _stitch_vtt_from_m3u8(playlist_url: str) -> str:
    """
    Download a captions .m3u8 and stitch all .vtt segments into a single VTT text.
    """
    print(f"[M3U8] Fetching captions playlist: {playlist_url}")

    limits = httpx.Limits(
        max_connections=CABLECAST_H2_MAX_CONN,
        max_keepalive_connections=CABLECAST_H2_MAX_KEEPALIVE,
    )
    timeout = httpx.Timeout(CABLECAST_FETCH_TIMEOUT)

    async with httpx.AsyncClient(http2=True, limits=limits, timeout=timeout) as client:
        pl = await client.get(playlist_url)
        pl.raise_for_status()
        base = playlist_url.rsplit("/", 1)[0] + "/"

        seg_urls = [
            urljoin(base, line.strip())
            for line in pl.text.splitlines()
            if line.strip() and not line.startswith("#")
        ]

        if not seg_urls:
            raise RuntimeError("Captions playlist contained no segments")

        print(f"[M3U8] Found {len(seg_urls)} caption segments")

        sem = asyncio.Semaphore(CABLECAST_H2_MAX_CONN)

        async def fetch_seg(idx: int, seg_url: str):
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

        results = await asyncio.gather(*[fetch_seg(i, u) for i, u in enumerate(seg_urls)])

    results.sort(key=lambda t: t[0])
    chunks = [txt for _, txt in results if txt]
    stitched_vtt = "WEBVTT\n\n" + "\n\n".join(chunks) + "\n"

    print(f"[M3U8] Stitched captions into {len(chunks)} segments")
    return stitched_vtt

# --------------------------
# Platform Handlers
# --------------------------

async def handle_granicus_url(page):
    """Trigger captions on Granicus (Dublin)."""
    print("[Granicus] Enabling captions...")
    await page.locator(".flowplayer").hover(timeout=10000)
    element = page.locator(".fp-menu").get_by_text("On", exact=True)
    await element.scroll_into_view_if_needed(timeout=10000)
    await element.click(force=True)
    print("[Granicus] Captions triggered")

async def handle_viebit_url(page):
    print("[Viebit] Enabling captions..123")

    try:
        await page.locator(".vjs-big-play-button").click(timeout=10000)
        print("[Viebit] Clicked big play button")
    except Exception as e:
        print(f"[Viebit] Big play button not found: {e}")

    try:
        await page.locator(".vjs-play-control").click(timeout=10000)
        print("[Viebit] Clicked play control")
    except Exception as e:
        print(f"[Viebit] Play control not found: {e}")

    await page.wait_for_timeout(500)

    try:
        await page.locator("button.vjs-subs-caps-button").click(timeout=10000)
        print("[Viebit] Opened captions menu")
    except Exception as e:
        print(f"[Viebit] Captions button not found: {e}")

    try:
        await page.locator('.vjs-menu-item:has-text("English")').click(timeout=10000)
        print("[Viebit] Selected English captions")
    except Exception as e:
        print(f"[Viebit] English captions not found: {e}")

    print("[Viebit] Finished handle_viebit_url()")

async def handle_viebit_url_old(page):
    """Trigger captions on Viebit (Fremont)."""
    print("[Viebit] Enabling captions...")
    await page.locator(".vjs-big-play-button").click(timeout=10000)
    await page.locator(".vjs-play-control").click(timeout=10000)
    await page.wait_for_timeout(500)
    await page.locator("button.vjs-subs-caps-button").click(timeout=10000)
    await page.locator('.vjs-menu-item:has-text("English")').click(timeout=10000)
    print("[Viebit] Captions triggered")


async def handle_cablecast_url(page):
    """Trigger captions on Cablecast."""
    print("[Cablecast] Enabling captions...")
    try:
        await page.locator(".vjs-big-play-button").click(timeout=8000)
    except Exception:
        try:
            await page.locator(".vjs-play-control").click(timeout=8000)
        except Exception:
            pass
    await page.wait_for_timeout(500)
    try:
        await page.locator(".vjs-subs-caps-button, .vjs-captions-button").click(timeout=8000)
        english = page.locator(".vjs-menu-item:has-text('English')")
        if await english.count() > 0:
            await english.first.click(timeout=8000)
        else:
            unchecked = page.locator(".vjs-menu-item[aria-checked='false']")
            if await unchecked.count() > 0:
                await unchecked.first.click(timeout=8000)
    except Exception:
        print("[Cablecast] Captions may already be enabled")


async def get_mp3_url(page):
    """Find MP3 download link in DOM."""
    try:
        mp3_href = await page.locator("a[href$='.mp3']").get_attribute("href")
        if mp3_href:
            print(f"[MP3] Found link: {mp3_href}")
            return mp3_href
    except Exception as e:
        print(f"[MP3] Not found: {e}")
    return None

async def get_mp4_url(page):
    """Find MP4 download link in DOM."""
    try:
        mp4_href = await page.locator("a[href$='.mp4']").get_attribute("href")
        if mp4_href:
            print(f"[MP4] Found link: {mp4_href}")
            return mp4_href
    except Exception as e:
        print(f"[MP4] Not found: {e}")
    return None


def get_hls_url_selenium(url: str, wait_time: int = 10) -> str:
    """
    Open page with Selenium, capture network requests, and return first .m3u8 URL.
    """
    print(f"[HLS-Selenium] Launching browser for {url}")
    driver = webdriver.Chrome()

    try:
        driver.get(url)
        time.sleep(wait_time)  # allow video player to load and request streams

        for request in driver.requests:
            if request.response and ".m3u8" in request.url and "captions" not in request.url.lower():
                print(f"[HLS-Selenium] Found stream: {request.url}")
                return request.url

        print("[HLS-Selenium] No .m3u8 found in network traffic")
        return None

    finally:
        driver.quit()


async def process_cvtv(url: str):
    """CVTV pipeline: get MP3 link → Whisper."""
    print("[CVTV] Processing...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")
        mp3_url = await get_mp3_url(page)
        await browser.close()

    if mp3_url:
        return download_and_transcribe_audio(mp3_url, prefer="mp3")
    return {"error": "No MP3 found for CVTV"}

# --------------------------
# Main
# --------------------------


async def fetch_transcript_for_url(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        loop = asyncio.get_event_loop()
        captions_future: asyncio.Future = loop.create_future()

        async def handle_response(response):
        if captions_future.done():
            return
        try:
            resp_url = (response.url or "").lower()
        except Exception:
            return

        # Catch .m3u8 captions
        if resp_url.endswith(".m3u8") and "captions" in resp_url:
            if not captions_future.done():
                captions_future.set_result(("m3u8", response.url))
            return

        # Catch .vtt captions
        if ".vtt" in resp_url:
            try:
                vtt_text = await response.text()
                if not captions_future.done():
                    captions_future.set_result(("vtt", vtt_text))
            except Exception as e:
                if not captions_future.done():
                    captions_future.set_exception(e)

        # ✅ New: Catch .mp4
        if ".mp4" in resp_url:
            if not captions_future.done():
                print(f"[Playwright] Found MP4 stream: {resp_url}")
                captions_future.set_result(("mp4", response.url))

        page.on("response", handle_response)

        try:
            print(f"[Playwright] Navigating to {url}")
            await page.goto(url, wait_until="load", timeout=45000)

            # --- Platform-specific triggers ---
            try:
                if "granicus.com" in url:
                    await handle_granicus_url(page)
                elif "viebit.com" in url:
                    try:
                        await handle_viebit_url(page)
                    except Exception as e:
                        print(f"[Viebit] Failed to enable captions: {e}")
                elif ".cablecast.tv" in url:
                    try:
                        await handle_cablecast_url(page)
                    except Exception as e:
                        print(f"[Cablecast] Failed to enable captions: {e}")
                elif ".cvtv.org" in url:
                    return await process_cvtv(url)
                else:
                    print("[Platform] Unknown, skipping handler")
            except Exception as e:
                print(f"[Platform] Error during handling: {e}")

            # --- Try to capture captions ---
            try:
                kind, payload = await asyncio.wait_for(captions_future, timeout=15)

                if kind == "vtt":
                    print("[Captions] VTT found")
                    return parse_vtt(payload)

                if kind == "m3u8":
                    print("[Captions] M3U8 captions found")
                    stitched_vtt_text = await _stitch_vtt_from_m3u8(payload)
                    return parse_vtt(stitched_vtt_text)

                if kind == "mp4":
                    print("[Playwright] Falling back to Whisper on MP4 stream...")
                    return await process_viebit_fallback(payload)  # use your mp4→whisper function

            except asyncio.TimeoutError:
                print("[Fallback] No captions found in 15s → trying audio")

                # --- MP3 fallback ---

                mp3_link = await get_mp3_url(page)
                if mp3_link:
                    print(f"[Fallback] Found MP3: {mp3_link}")
                    return download_and_transcribe_audio(mp3_link, prefer="mp3")
                else:
                    print("[Fallback] No MP3 link found in page")

                # --- MP4 fallback ---
                mp4_link = await get_mp4_url(page)
                if mp4_link:
                    print(f"[Fallback] Found MP4: {mp4_link}")
                    return download_and_transcribe_audio(mp4_link, prefer="mp4")
                else:
                    print("[Fallback] No MP4 link found in page")

                # --- HLS video fallback ---
                print("[Fallback] Looking for HLS video streams...")
                m3u8_link = get_hls_url_generic(url)   # <- direct page URL

                if m3u8_link:
                    print(f"[Fallback] Found HLS video: {m3u8_link}")
                    uid = str(uuid.uuid4())
                    mp4_file = f"video_{uid}.mp4"
                    local_mp4 = download_from_m3u8(m3u8_link, mp4_file)
                    if local_mp4:
                        return download_and_transcribe_audio(local_mp4, prefer="mp4")
                else:
                    print("[Fallback] No HLS stream found via Selenium")


                raise ValueError("No transcript, MP3, MP4, or HLS streams found.")

        finally:
            await browser.close()



async def fetch_transcript_for_url_old(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        loop = asyncio.get_event_loop()
        captions_future: asyncio.Future = loop.create_future()

        async def handle_response(response):
            if captions_future.done():
                return
            resp_url = (response.url or "").lower()
            if resp_url.endswith(".m3u8") and "captions" in resp_url:
                captions_future.set_result(("m3u8", response.url))
            elif ".vtt" in resp_url:
                vtt_text = await response.text()
                captions_future.set_result(("vtt", vtt_text))

        page.on("response", handle_response)

        try:
            print(f"[Playwright] Navigating to {url}")
            await page.goto(url, wait_until="load", timeout=45000)

            if "granicus.com" in url:
                await handle_granicus_url(page)
            elif "viebit.com" in url:
                await handle_viebit_url(page)
            elif ".cablecast.tv" in url:
                await handle_cablecast_url(page)
            elif ".cvtv.org" in url:
                return await process_cvtv(url)
            else:
                raise ValueError("Unknown platform. Could not process URL.")

            try:
                kind, payload = await asyncio.wait_for(captions_future, timeout=15)
                if kind == "vtt":
                    print("[Captions] VTT found")
                    return parse_vtt(payload)
                if kind == "m3u8":
                    print("[Captions] M3U8 captions found")
                    stitched_vtt_text = await _stitch_vtt_from_m3u8(payload)
                    return parse_vtt(stitched_vtt_text)
            except asyncio.TimeoutError:
                print("[Fallback] No captions found, trying audio...")

                # MP3
                mp3_link = await get_mp3_url(page)
                if mp3_link:
                    return download_and_transcribe_audio(mp3_link, prefer="mp3")

                # MP4
                mp4_link = await page.locator("a[href$='.mp4']").get_attribute("href")
                if mp4_link:
                    print(f"[Fallback] Found MP4: {mp4_link}")
                    return download_and_transcribe_audio(mp4_link, prefer="mp4")

                # HLS
                print("[Fallback] Looking for HLS video streams...")
                m3u8_link = await get_hls_url_selenium(page)

                if m3u8_link:
                    print(f"[Fallback] Found HLS video: {m3u8_link}")
                    uid = str(uuid.uuid4())
                    mp4_file = f"video_{uid}.mp4"
                    local_mp4 = download_from_m3u8(m3u8_link, mp4_file)
                    if local_mp4:
                        return download_and_transcribe_audio(local_mp4, prefer="mp4")
                else:
                    print("[Fallback] No HLS stream found in DOM, HTML, or network")

        finally:
            await browser.close()
