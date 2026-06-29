"""The single naming path for VOD2MLIB.

Provider-supplied VOD names are noisy (quality/language prefixes, embedded years,
junk tags, list numbers, dotted release names). `parse_title` normalises them into
a clean title + year so media-server agents (Plex Movie / Plex TV Series, Jellyfin,
Emby, Kodi, ChannelsDVR) match reliably. External IDs (tmdb/imdb) are emitted
one-per-brace the way Plex requires.

This module is the authority for BOTH outputs:
- the HTTP mount (mountsrv/) builds directory/file names from it;
- the .strm generator (plugin.py) builds folder/.strm names from it.

It is pure (no Django, no I/O) so it is unit-testable in isolation and importable
in-process before the mount's child-only deps exist.

Origin: the robust normaliser is VODFS's (https://github.com/OneHotTake/vodfs, MIT,
© 2026 OneHotTake), promoted here as the canonical engine for the whole plugin.
"""

import re
from typing import Any, Dict, Optional

# --- Provider-name normalisation -------------------------------------------------

# Language codes that may appear in a provider prefix; mapped to ISO-639-1 later.
_LANG = {
    'EN', 'FR', 'DE', 'IT', 'ES', 'PT', 'NL', 'PL', 'RU', 'AR', 'TR', 'SV', 'SE',
    'NO', 'DA', 'DK', 'FI', 'EL', 'GR', 'RO', 'CS', 'CZ', 'HU', 'HE', 'HI', 'JA',
    'KO', 'ZH', 'UK', 'BG', 'HR', 'SR', 'MULTI', 'MULTISUB', 'VOSTFR', 'VOST',
    'VOSE', 'LAT', 'VO', 'DUAL',
}
# Quality / provider tags that may appear in a prefix (D+, A+ = Disney+/Apple TV+).
_QUAL = {
    '4K', 'UHD', 'HD', 'FHD', 'SD', 'HDR', 'HDR10', 'DV', '3D', 'HEVC', 'H265',
    'X265', 'H264', 'X264', '1080P', '720P', '2160P', '480P', 'HDTS', 'HDCAM',
    'CAM', 'TS', 'WEB', 'WEBDL', 'WEBRIP', 'BLURAY', 'BRRIP', 'DVDRIP', 'REMUX',
    'D+', 'A+', 'N', 'P+', 'HBO', 'MAX',
}
_LANG_MAP = {
    'MULTI': 'mul', 'MULTISUB': 'mul', 'VOST': 'mul', 'VOSTFR': 'fr', 'VOSE': 'es',
    'LAT': 'es', 'SE': 'sv', 'DK': 'da', 'GR': 'el', 'CZ': 'cs',
}
_YEAR_RE = re.compile(r'(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)')
# A *parenthesised/bracketed* year is the canonical release marker. Anything after
# the first one is provider junk (duplicate years, cast credits) we truncate away.
_PAREN_YEAR_RE = re.compile(r'[(\[]\s*((?:19|20)\d{2})\s*[)\]]')
# Resolution/codec/source tokens that providers wedge mid-title. Deliberately a
# *narrower* set than _QUAL: ambiguous real-word codes (MAX, HBO, CAM, WEB, TS, DV)
# are excluded so titles like "Mad Max" survive an inline strip.
_QUAL_INLINE = {
    '4K', 'UHD', 'FHD', 'QHD', 'HDR', 'HDR10', 'HEVC', 'H265', 'X265', 'H264',
    'X264', 'XVID', '1080P', '720P', '2160P', '480P', '4320P', 'HDTS', 'HDCAM',
    'WEBDL', 'WEBRIP', 'BRRIP', 'BLURAY', 'DVDRIP', 'REMUX',
}
_TAG_RE = re.compile(
    r'[\[(]\s*(?:MULTI[- ]?SUB|MULTISUB|SUB|DUAL|VOST(?:FR|E)?|HDTS|HDCAM|CAM|HDR|'
    r'HEVC|MAIN CARD|PRELIMS|EARLY PRELIMS|UNCUT|EXTENDED|REMASTERED|IMAX|3D|REPACK|'
    r'\d{3,4}P)\s*[\])]', re.IGNORECASE)
# Characters that are illegal/awkward in file names on common filesystems.
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _looks_like_prefix(token: str) -> bool:
    """Is a pre-delimiter token a provider/quality/language code, not a real title?"""
    t = (token or '').strip()
    if not t or len(t) > 12:
        return False
    parts = [p for p in re.split(r'[-\s|]+', t.upper()) if p]
    if not parts:
        return False
    for p in parts:
        if p in _QUAL or p in _LANG:
            continue
        if re.fullmatch(r'[A-Z0-9]{1,4}\+?', p):  # short codes: 4K, EN, D+, N, P+
            continue
        return False
    return True


def _extract_lang(prefix: str) -> Optional[str]:
    for p in re.split(r'[-\s|]+', (prefix or '').upper()):
        if p in _LANG:
            return _LANG_MAP.get(p, p.lower()[:2])
    return None


def parse_title(raw: str, year_field: Any = None) -> Dict[str, Any]:
    """Normalise a provider VOD name into {title, year, language}."""
    name = re.sub(r'\s+', ' ', (raw or '').strip())
    language = None

    # 1. Strip a leading provider/quality/language prefix delimited by '|' or ' - '.
    if '|' in name:
        left, right = name.split('|', 1)
        if _looks_like_prefix(left):
            language = _extract_lang(left)
            name = right.strip()
    if ' - ' in name:
        left, right = name.split(' - ', 1)
        if _looks_like_prefix(left):
            language = language or _extract_lang(left)
            name = right.strip()

    # Strip a leading list-number prefix ("117. Die Hard" -> "Die Hard").
    name = re.sub(r'^\d{1,4}\.\s+', '', name)

    # 2. Year: prefer a sane year field, else extract from the name.
    has_year_field = isinstance(year_field, int) and 1900 <= year_field <= 2100
    year = year_field if has_year_field else None
    if ' ' not in name and name.count('.') >= 2:  # dotted release name
        name = name.replace('.', ' ')
    name_before_year = name
    paren = _PAREN_YEAR_RE.search(name)
    if paren:
        # Truncate at the first parenthesised year so "Cool Hand Luke 4K (1967)
        # PAUL NEWMAN (1967)" yields "Cool Hand Luke" rather than a folder name
        # scrapers can't match. A bare year that is part of the title (e.g.
        # "Blade Runner 2049") sits before the paren and is preserved.
        if year is None:
            year = int(paren.group(1))
        work = name[:paren.start()]
    else:
        matches = _YEAR_RE.findall(name)
        last = int(matches[-1]) if matches else None
        work = name
        if last is not None:
            if year is None:
                # No known year: the bare year IS the release year — adopt it and
                # strip the token. "Wicked For Good 2025" -> "Wicked For Good" (2025).
                year = last
                work = re.sub(r'[\(\[]?\b' + matches[-1] + r'\b[\)\]]?', ' ', name)
            elif last == year:
                # Known year and the bare token duplicates it — strip the redundant copy.
                work = re.sub(r'[\(\[]?\b' + matches[-1] + r'\b[\)\]]?', ' ', name)
            # else: a bare number that is NOT the release year is part of the title
            # (Blade Runner 2049, Room 1408) — keep it.

    work = _finalize(work)
    if not work:
        # The whole title was a year-like token (e.g. a movie named "1992").
        work = _finalize(name_before_year)
        if not has_year_field:
            year = None

    return {'title': work, 'year': year, 'language': language}


def _strip_inline_quality(name: str) -> str:
    """Drop standalone resolution/codec/source tokens (4K, 1080p, HEVC, BluRay) that
    providers leave inside a title. Only unambiguous tokens in _QUAL_INLINE are
    removed, so real-word codes (Max, HBO, Cam) stay put."""
    return re.sub(
        r'\b[A-Za-z0-9]{2,7}\b',
        lambda m: ' ' if m.group(0).upper() in _QUAL_INLINE else m.group(0),
        name,
    )


def _finalize(name: str) -> str:
    """Strip junk tags, bracket groups, trailing country codes; collapse whitespace."""
    name = _TAG_RE.sub(' ', name)
    name = re.sub(r'\[[^\]]*\]', ' ', name)                  # all [tag] groups
    name = _strip_inline_quality(name)                       # mid-title 4K/1080p/...
    name = re.sub(r'\s*\((?:[A-Za-z]{2})\)\s*$', ' ', name)  # trailing country code
    name = re.sub(r'[\[(]\s*[\])]', ' ', name)               # empty brackets
    return re.sub(r'\s+', ' ', name).strip(' -._')


def sanitize_filename(name: str) -> str:
    """Make a tree component safe as a single path segment.

    Illegal chars become spaces (keeps word boundaries readable), runs of
    whitespace collapse, and the bare traversal components '.'/'..' (or an empty
    result) fall back to 'Unknown' so a provider-supplied name can never escape
    the library root when joined to a path.
    """
    name = _ILLEGAL_FS.sub(' ', name or '')
    name = re.sub(r'\s+', ' ', name).strip()
    return 'Unknown' if name in ('', '.', '..') else name


def format_external_ids(tmdb_id: Any = None, imdb_id: Any = None) -> str:
    """Return ' {tmdb-N} {imdb-ttX}' with each id in its own brace (Plex requirement)."""
    ids = []
    if tmdb_id:
        ids.append('{tmdb-%s}' % str(tmdb_id).strip())
    if imdb_id:
        v = str(imdb_id).strip()
        if v:
            v = v if v.startswith('tt') else 'tt' + v
            ids.append('{imdb-%s}' % v)
    return (' ' + ' '.join(ids)) if ids else ''


def _title_with_year(parsed: Dict[str, Any]) -> str:
    base = parsed['title']
    if parsed.get('year'):
        base += ' (%d)' % parsed['year']
    return base


def folder_name(name, year=None, tmdb_id=None, imdb_id=None) -> str:
    """The canonical library folder name for a movie or series.

    'Title (Year) {tmdb-N} {imdb-ttX}' — external-id braces always included when
    known, because that is what makes scraper matching exact. Used by the mount
    (directory name) and the .strm generator (folder name) alike, so a title lands
    at the same folder name no matter which output produced it.
    """
    p = parse_title(name, year)
    return sanitize_filename(_title_with_year(p) + format_external_ids(tmdb_id, imdb_id))


def season_dir_name(season_number: int) -> str:
    try:
        return 'Season %02d' % int(season_number)
    except (TypeError, ValueError):
        return 'Season 01'


def episode_display_title(episode_name: str) -> str:
    """Extract just the episode title from a provider episode name.

    'A+ - Berlin ER (2025) (DE) - S01E01 - Symptoms' -> 'Symptoms'
    """
    if not episode_name:
        return ''
    m = re.search(r'[Ss]\d{1,3}[Ee]\d{1,4}\s*-\s*(.+)$', episode_name)
    if m:
        return sanitize_filename(m.group(1).strip())
    parts = episode_name.rsplit(' - ', 1)
    tail = parts[-1].strip() if len(parts) > 1 else ''
    if tail and not re.match(r'^[Ss]\d', tail):
        return sanitize_filename(tail)
    return ''


def _ext_of(relation) -> str:
    return (getattr(relation, 'container_extension', None) or 'mkv').lstrip('.')


def _provider_suffix(provider_label: str, stream_id) -> str:
    """' - <provider> - <stream_id>' (or just ' - <stream_id>'); the stream_id
    makes a mount filename unique and lets it resolve back to one provider stream."""
    if provider_label:
        return ' - %s - %s' % (provider_label, stream_id)
    return ' - %s' % stream_id


# --- mount file names (one file per provider stream) -----------------------------

def movie_filename(name, year, tmdb_id, imdb_id, relation, provider_label='') -> str:
    p = parse_title(name, year)
    base = _title_with_year(p) + format_external_ids(tmdb_id, imdb_id)
    sid = getattr(relation, 'stream_id', '')
    return sanitize_filename(base + _provider_suffix(provider_label, sid)) + '.' + _ext_of(relation)


def episode_filename(series_name, series_year, season_number, episode_number,
                     episode_name, relation, provider_label='') -> str:
    p = parse_title(series_name, series_year)
    head = _title_with_year(p)
    se = 'S%02dE%02d' % (int(season_number or 0), int(episode_number or 0))
    ep_title = episode_display_title(episode_name)
    out = '%s - %s' % (head, se)
    if ep_title:
        out += ' - %s' % ep_title
    sid = getattr(relation, 'stream_id', '')
    out += _provider_suffix(provider_label, sid)
    return sanitize_filename(out) + '.' + _ext_of(relation)


# --- .strm file names (one file per movie/episode) -------------------------------

def strm_filename(folder_base: str) -> str:
    """The .strm filename for a movie folder — matches the folder so scrapers and
    the file agree. `folder_base` is the folder name (already sanitised)."""
    return '%s.strm' % folder_base


def episode_basename(series_name, series_year, season_number, episode_number,
                     episode_name) -> str:
    """SxxEyy base name for an episode (no extension, no provider/stream-id suffix —
    the stream lives inside the .strm's URL, not in the filename). Caller appends
    '.strm'/'.nfo'. Identical title head to the mount's episode_filename, so a .strm
    library and the mount name the same episode the same way."""
    p = parse_title(series_name, series_year)
    head = _title_with_year(p)
    se = 'S%02dE%02d' % (int(season_number or 0), int(episode_number or 0))
    ep_title = episode_display_title(episode_name)
    out = '%s - %s' % (head, se)
    if ep_title:
        out += ' - %s' % ep_title
    return sanitize_filename(out)


def category_segment(category_name: str, nest: bool) -> str:
    """Category subfolder segment for the .strm layout, '' when not nesting."""
    if not nest:
        return ''
    cat = (category_name or '').strip()
    return sanitize_filename(cat) if cat else 'Unassigned'
