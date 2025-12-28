# app/utils.py
import re
import html


_TIMESTAMP_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}(?::\d{2})?\.\d{3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?\.\d{3}.*$"
)
_SRT_TIMESTAMP_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{3}.*$"
)
_CUE_ID_RE = re.compile(r"^\s*(?:\d+|[a-zA-Z0-9_-]{3,})\s*$")  # optional; safe-ish
_TAG_RE = re.compile(r"<[^>]+>")  # remove any HTML/VTT tags like <c>, <v>, <i>, etc.
_SPACES_RE = re.compile(r"[ \t]+")

def parse_vtt(vtt_content: str) -> str:
    """
    Parses raw VTT content to extract clean subtitle text.
    This version is robust enough for both platforms.
    """
    lines = vtt_content.strip().split('\n')
    transcript_lines = []
    seen_lines = set()

    for line in lines:
        if not line.strip() or "WEBVTT" in line or "-->" in line or line.strip().isdigit():
            continue
        
        # Clean announcer tags from Granicus and extra whitespace
        cleaned_line = re.sub(r'>>\s*', '', line).strip()
        
        if cleaned_line and cleaned_line not in seen_lines:
            transcript_lines.append(cleaned_line)
            seen_lines.add(cleaned_line)
            
    return "\n".join(transcript_lines)



def parse_youtube_vtt(vtt_or_srt_text: str) -> str:
    """
    Extract clean readable text from .vtt or .srt content:
    - strips WEBVTT header + NOTE/STYLE/REGION blocks
    - removes timestamps + cue settings
    - removes tags (<c>, <v>, <i>, etc) and decodes HTML entities
    - collapses whitespace
    - removes duplicate consecutive lines
    """
    if not vtt_or_srt_text:
        return ""

    # Normalize newlines
    text = vtt_or_srt_text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    out = []
    prev = None

    in_note = False
    in_style = False
    in_region = False

    for raw in lines:
        line = raw.strip()

        # Skip empty quickly (but keep paragraph breaks later if you want)
        if not line:
            continue

        # Skip WEBVTT header and metadata lines
        if line.upper().startswith("WEBVTT"):
            continue
        if line.upper().startswith("X-TIMESTAMP-MAP"):
            continue

        # Block handling: NOTE / STYLE / REGION
        upper = line.upper()
        if upper.startswith("NOTE"):
            in_note = True
            continue
        if upper.startswith("STYLE"):
            in_style = True
            continue
        if upper.startswith("REGION"):
            in_region = True
            continue

        # End of NOTE/STYLE/REGION blocks is typically a blank line;
        # since we skipped blanks, we also end block when we hit a timestamp cue.
        if in_note or in_style or in_region:
            # If we encounter a cue timestamp, end the block and process normally
            if _TIMESTAMP_RE.match(line) or _SRT_TIMESTAMP_RE.match(line):
                in_note = in_style = in_region = False
            else:
                continue

        # Skip timestamp cue lines
        if _TIMESTAMP_RE.match(line) or _SRT_TIMESTAMP_RE.match(line):
            continue

        # Skip typical cue settings lines (rare) or arrows by themselves
        if "-->" in line:
            continue

        # Skip numeric-only SRT cue indices
        if line.isdigit():
            continue

        # Sometimes cue IDs appear on a line alone before timestamps.
        # This is optional: keep conservative to avoid dropping real content.
        # We'll only drop if it's short-ish and looks like an ID.
        if len(line) <= 32 and _CUE_ID_RE.match(line):
            # If it's likely an ID and NOT a normal sentence.
            # Avoid removing lines with spaces (sentences).
            if " " not in line:
                continue

        # Remove tags like <c>, <v Speaker>, <i>, </c>, etc
        line = _TAG_RE.sub("", line)

        # Decode HTML entities (&amp;, &#39;, etc.)
        line = html.unescape(line)

        # Remove stray music notes / caption markers if you want
        # line = line.replace("♪", "")

        # Collapse whitespace
        line = _SPACES_RE.sub(" ", line).strip()

        # Drop tiny artifacts
        if not line:
            continue

        # Deduplicate consecutive duplicates
        if prev is not None and line == prev:
            continue
        prev = line
        out.append(line)

    # Join as paragraphs (one line per caption)
    return "\n".join(out).strip()


def sanitize_filename(name: str) -> str:
    """Removes characters that are invalid in filenames."""
    sanitized = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return (sanitized[:150] + '...') if len(sanitized) > 150 else sanitized

def extract_youtube_video_id(url: str) -> str | None:
    """
    Extracts the 11-character YouTube video ID from a URL.
    Handles standard, short, and embed URLs.
    """
    # Standard and short URLs (and others)
    patterns = [
        r"(?:v=|\/v\/|youtu\.be\/|embed\/)([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def parse_srt(srt_text: str) -> str:
    """
    Simple .srt parser that joins subtitle lines into plain text.
    """
    lines = []
    for line in srt_text.splitlines():
        line = line.strip()
        # Skip numbers and timestamps
        if not line or line.isdigit() or "-->" in line:
            continue
        lines.append(line)
    return "\n".join(lines)
