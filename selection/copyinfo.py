"""What a copy is: quality, codec, bitrate and language, from data the
provider already sent (source "provider"). No network calls.

Every field is optional; an empty `langs` list means unknown, not "none".
A deep probe (probe.py) later overlays the fields it measured (merge_probe).

Where it comes from:
  * video (width/height/codec) and bitrate: the movie's `detailed_info`, or
    for a series the `info` of one of the copy's episodes (only present once
    its episodes have been fetched).
  * quality tokens ("4K", "1080p", ...) in the copy's own name or category,
    when there is no video data.
  * language: tags in the copy's own name, then its category ("EN - ",
    "|FR|", "▪NL▪", "EN-TOP - "). The provider's audio stream data is not
    used: it lists a single track, which is often not the copy's language.
"""
import re

QUALITIES = ("2160", "1080", "720", "SD")

_BULLETS = "▪▫■□●○•◦‣⁃"
_LANG_TAG_RES = (
    re.compile(r"^([A-Z]{2,3})\s+-"),                        # EN - Title
    re.compile(r"^\|?\s*([A-Z]{2,3})\s*\|"),                  # EN| Title, |EN| Title
    re.compile(r"^\[([A-Z]{2,3})\]"),                        # [EN] Title
    re.compile(r"^[" + _BULLETS + r"]+\s*([A-Za-z]{2,8})\s*[" + _BULLETS + r"]+"),  # ▪NL▪ Title
    re.compile(r"^([A-Z]{2,3})-[A-Z]{2,6}\b"),                # EN-TOP - 02. Title
    re.compile(r"^(EN)\s+"),                                 # EN Title (EN only)
)
# Three-letter and country codes providers use for a language.
_LANG_ALIASES = {
    "ENG": "EN", "UK": "EN", "US": "EN", "GER": "DE", "DEU": "DE", "FRE": "FR", "FRA": "FR",
    "SPA": "ES", "ESP": "ES", "ITA": "IT", "POR": "PT", "NLD": "NL", "DUT": "NL", "TUR": "TR",
    "ARA": "AR", "RUS": "RU", "POL": "PL", "HIN": "HI", "SWE": "SV", "NOR": "NO", "DAN": "DA",
    "FIN": "FI", "GRE": "EL", "HEB": "HE", "KOR": "KO", "JPN": "JA", "CHI": "ZH", "LAT": "ES",
}
# Two-letter tags that are not languages.
_NOT_LANGS = {"TV", "HD", "SD", "VO", "OV", "XX"}

_QUALITY_TOKENS = (
    ("2160", re.compile(r"\b(?:4K|UHD|2160p)\b", re.IGNORECASE)),
    ("1080", re.compile(r"\b(?:1080p|FHD|FULL\s?HD)\b", re.IGNORECASE)),
    ("720", re.compile(r"\b720p\b", re.IGNORECASE)),
    ("SD", re.compile(r"\b(?:SD|480p|576p)\b")),
)


def language_of(text):
    """The language tag leading `text`, normalised to a two-letter code
    (or "MULTI"), or None."""
    text = (text or "").strip()
    for rx in _LANG_TAG_RES:
        m = rx.match(text)
        if not m:
            continue
        code = m.group(1).upper()
        if code.startswith("MULTI"):
            return "MULTI"
        code = _LANG_ALIASES.get(code, code)
        if len(code) == 2 and code not in _NOT_LANGS:
            return code
    return None


def quality_of_video(width, height):
    """Quality class from frame size. Width first: scope films are 1920x800
    and still 1080p."""
    width, height = _int(width), _int(height)
    if not width and not height:
        return None
    width, height = width or 0, height or 0
    if width >= 3200 or height >= 2000:
        return "2160"
    if width >= 1800 or height >= 1000:
        return "1080"
    if width >= 1200 or height >= 700:
        return "720"
    return "SD"


def quality_of_text(*texts):
    for quality, rx in _QUALITY_TOKENS:
        if any(rx.search(t or "") for t in texts):
            return quality
    return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def copy_info(name, category, media):
    """CopyInfo for one copy.

    `name` is the copy's own name, `category` its category name, and `media`
    the provider's info block that may hold `video` and `bitrate` (or None).
    """
    media = media if isinstance(media, dict) else {}
    video = media.get("video") if isinstance(media.get("video"), dict) else {}
    quality = quality_of_video(video.get("width"), video.get("height")) or quality_of_text(name, category)
    lang = language_of(name) or language_of(category)
    return {
        "quality": quality,
        "height": _int(video.get("height")),
        "video_codec": (video.get("codec_name") or None) and str(video["codec_name"]).upper(),
        "bitrate_kbps": _int(media.get("bitrate")) or None,
        "langs": [lang] if lang else [],
        "subtitle_langs": [],
        "source": "provider",
    }


def merge_probe(info, probe):
    """Overlay a stored deep-probe result (see store.probes) on a copy's
    provider info. A probe's languages replace tag languages only when its
    tracks carried language tags; its errors are passed along as-is."""
    if not probe:
        return info
    info = dict(info, probed_at=probe.get("probed_at"), probe_error=probe.get("error"))
    result = probe.get("result")
    if result:
        for key in ("quality", "height", "video_codec"):
            if result.get(key):
                info[key] = result[key]
        if result.get("langs"):
            info["langs"] = result["langs"]
        info["subtitle_langs"] = result.get("subtitle_langs") or []
        info["source"] = "deep"
    return info


DEFAULT_PREFS = {"audio": [], "subtitles": [], "quality_order": list(QUALITIES)}

MATCH, UNKNOWN, MISMATCH = 0, 1, 2


def _dimension(wanted, have, known):
    if not wanted:
        return MATCH
    if not known:
        return UNKNOWN
    return MATCH if set(wanted) & set(have) else MISMATCH


def language_match(info, prefs):
    """MATCH / UNKNOWN / MISMATCH against the audio toggles.
    Audio languages are known once a probe or a name tag gave any.
    Subtitles are a preference only (subtitle_match):
    a copy without them must not be marked as not matching."""
    info = info or {}
    audio = _dimension(prefs.get("audio"), info.get("langs") or [], bool(info.get("langs")))
    if not prefs.get("subtitles_required"):
        return audio
    # Both needed: the worse of the two. A probed copy with no subtitle tracks
    # at all counts as unknown, not missing: its subtitles may be burned in
    # (e.g. French films in an "EN" category).
    subs = _dimension(prefs.get("subtitles"), info.get("subtitle_langs") or [],
                      info.get("source") == "deep" and bool(info.get("subtitle_langs")))
    return max(audio, subs)


def title_prefs(prefs, audio_override):
    """The preferences for one title. A per-title override (a film preferred
    in its original language) asks for that audio language only, so English
    audio neither helps nor hurts, and makes the global subtitle languages a
    requirement instead of a preference."""
    if not audio_override:
        return prefs
    return dict(prefs, audio=[audio_override.upper()], subtitles_required=True)


def subtitle_match(info, prefs):
    """MATCH / UNKNOWN / MISMATCH against the preferred subtitle languages;
    known only after a probe (providers never list subtitles)."""
    info = info or {}
    return _dimension(prefs.get("subtitles"), info.get("subtitle_langs") or [],
                      info.get("source") == "deep")


def default_audio_ok(info, prefs):
    """False when a probe shows the default audio track isn't a preferred
    language (players start on it), None when that isn't known."""
    info = info or {}
    wanted = prefs.get("audio") or []
    if not wanted or info.get("source") != "deep" or not info.get("langs"):
        return None
    return info["langs"][0] in wanted


def rank_key(copy, prefs=None):
    """Sort key, best copy first: audio languages match, quality (in the
    user's order), preferred subtitles, default audio track preferred,
    bitrate, probed over unprobed, then a stable tiebreaker. Without audio
    toggles every copy matches, so quality leads."""
    prefs = prefs or DEFAULT_PREFS
    info = copy.get("info") or {}
    order = prefs.get("quality_order") or list(QUALITIES)
    quality = info.get("quality")
    return (
        language_match(info, prefs),
        order.index(quality) if quality in order else len(order),
        subtitle_match(info, prefs),
        0 if default_audio_ok(info, prefs) is not False else 1,
        -(info.get("bitrate_kbps") or 0),
        0 if info.get("source") == "deep" else 1,
        copy.get("account_id") or 0,
        str(copy.get("stream_id")),
    )


def rank(copies, prefs=None):
    return sorted(copies, key=lambda c: rank_key(c, prefs))


def clean_prefs(raw):
    """Validated preferences from page input. Raises ValueError."""
    prefs = dict(DEFAULT_PREFS)
    for key in ("audio", "subtitles"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} must be a list of language codes")
        prefs[key] = sorted({v.strip().upper() for v in value if v.strip()})
    order = raw.get("quality_order", list(QUALITIES))
    if not isinstance(order, list) or any(q not in QUALITIES for q in order):
        raise ValueError(f"quality_order must list qualities from {list(QUALITIES)}")
    order = list(dict.fromkeys(order))
    prefs["quality_order"] = order + [q for q in QUALITIES if q not in order]
    return prefs
