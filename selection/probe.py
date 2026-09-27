"""Deep probe: ffprobe a copy through Dispatcharr's own VOD proxy
(source "deep"). It opens a real stream, so it reports the actual audio and
subtitle tracks and frame size, which provider data often gets wrong.

Going through the proxy (not the provider URL) keeps Dispatcharr's
per-account connection limits in force. Probes run one at a time; a proxy
"no capacity" reply (the account's streams are all in use, or the previous
probe's connection hasn't been released yet) is retried after a short wait.
"""
import json
import os
import subprocess
import threading
import time

from .copyinfo import quality_of_video

# ISO 639-2 (as ffprobe reports them) -> the two-letter codes used elsewhere.
_LANG3 = {
    "eng": "EN", "ger": "DE", "deu": "DE", "fre": "FR", "fra": "FR", "spa": "ES", "ita": "IT",
    "por": "PT", "dut": "NL", "nld": "NL", "cze": "CS", "ces": "CS", "slo": "SK", "slk": "SK",
    "pol": "PL", "rus": "RU", "ukr": "UK", "swe": "SV", "dan": "DA", "nor": "NO", "nob": "NO",
    "nno": "NO", "fin": "FI", "ice": "IS", "isl": "IS", "gre": "EL", "ell": "EL", "tur": "TR",
    "ara": "AR", "heb": "HE", "hin": "HI", "jpn": "JA", "kor": "KO", "chi": "ZH", "zho": "ZH",
    "hun": "HU", "rum": "RO", "ron": "RO", "bul": "BG", "hrv": "HR", "srp": "SR", "slv": "SL",
    "est": "ET", "lav": "LV", "lit": "LT", "tha": "TH", "vie": "VI", "ind": "ID", "may": "MS",
    "msa": "MS", "per": "FA", "fas": "FA", "cat": "CA", "baq": "EU", "eus": "EU", "glg": "GL",
    "tam": "TA", "tel": "TE", "fil": "TL", "tgl": "TL", "alb": "SQ", "sqi": "SQ", "mac": "MK",
    "mkd": "MK", "bos": "BS", "wel": "CY", "cym": "CY", "gle": "GA",
}
_NO_LANG = {"", "und", "unk", "mis", "mul", "zxx", "qaa"}

FFPROBE_ARGS = [
    "ffprobe", "-v", "error", "-rw_timeout", "15000000",
    "-show_entries",
    "stream=codec_type,codec_name,width,height,channels"
    ":stream_tags=language:stream_disposition=default,forced",
    "-of", "json",
]
TIMEOUT = 60
BUSY_RETRIES = 4
BUSY_DELAY = 2.0

_lock = threading.Lock()  # one probe at a time, per process


class ProbeError(Exception):
    def __init__(self, message, busy=False):
        super().__init__(message)
        self.busy = busy  # the provider had no free connection


def lang_code(tag):
    tag = (tag or "").strip().lower()
    if tag in _NO_LANG:
        return None
    if len(tag) == 2:
        return tag.upper()
    return _LANG3.get(tag, tag.upper())


def _langs(streams):
    """Distinct languages, the default track first; tracks without a
    language tag are skipped."""
    ordered = sorted(streams, key=lambda s: not (s.get("disposition") or {}).get("default"))
    out = []
    for s in ordered:
        code = lang_code((s.get("tags") or {}).get("language"))
        if code and code not in out:
            out.append(code)
    return out


def parse(ffprobe_json):
    """The fields of CopyInfo a probe knows, from ffprobe's JSON output."""
    streams = ffprobe_json.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    return {
        "quality": quality_of_video(video.get("width"), video.get("height")),
        "height": video.get("height"),
        "video_codec": (video.get("codec_name") or "").upper() or None,
        "langs": _langs(audio),
        "audio_tracks": len(audio),
        "subtitle_langs": _langs(subs),
    }


def internal_base_url():
    """Dispatcharr's web server as seen from inside its own container."""
    return f"http://127.0.0.1:{os.environ.get('DISPATCHARR_PORT') or 9191}"


def run(url, runner=subprocess.run, sleep=time.sleep):
    """ffprobe `url`; returns parse()d info. Raises ProbeError."""
    with _lock:
        for attempt in range(BUSY_RETRIES + 1):
            try:
                p = runner(FFPROBE_ARGS + [url], capture_output=True, text=True, timeout=TIMEOUT)
            except FileNotFoundError:
                raise ProbeError("ffprobe is not installed")
            except subprocess.TimeoutExpired:
                raise ProbeError(f"no answer from the stream within {TIMEOUT}s")
            if p.returncode == 0:
                try:
                    info = parse(json.loads(p.stdout or "{}"))
                except ValueError:
                    raise ProbeError("ffprobe returned unreadable output")
                if not info["quality"] and not info["audio_tracks"]:
                    raise ProbeError("the stream has no video or audio tracks")
                return info
            busy = "503" in (p.stderr or "") or "5XX" in (p.stderr or "")
            if not busy:
                lines = (p.stderr or "").strip().splitlines()
                raise ProbeError((lines[-1] if lines else "ffprobe failed")[:300])
            if attempt < BUSY_RETRIES:
                sleep(BUSY_DELAY)
        raise ProbeError("the provider has no free connection (all in use); try again later", busy=True)
