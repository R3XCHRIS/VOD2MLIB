"""Dispatcharr VOD data access for the HTTP mount.

Naming and proxy-URL formation are NOT defined here — they live in the shared core
(`vodlib.naming`, `vodlib.playback`) and are used identically by the .strm
generator. This module is only the mount's *data* layer: live ORM queries for
episodes and the provider-metadata size logic Plex needs to play.
"""

import os
import json
import logging
import threading
import urllib.request
from collections import defaultdict
from typing import List, Dict, Any, Optional

try:
    from apps.vod.models import Series, Episode
    from apps.vod.models import M3USeriesRelation, M3UEpisodeRelation
    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False
    Series = Episode = None
    M3USeriesRelation = M3UEpisodeRelation = None

from vodlib import naming as _naming
from vodlib.playback import proxy_url as _proxy_url

# Re-export the canonical normaliser so the mount's tree.py keeps one import site.
parse_title = _naming.parse_title

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:9191"


def _get_dispatcharr_base_url() -> str:
    """Operator-configured Dispatcharr base URL (the proxy/probe target)."""
    url = os.environ.get("VOD2MLIB_DISPATCHARR_URL", _DEFAULT_BASE_URL).rstrip("/")
    try:
        from urllib.parse import urlparse
        if urlparse(url).scheme not in ("http", "https"):
            logger.warning("Ignoring non-HTTP base URL; using default")
            return _DEFAULT_BASE_URL
    except Exception:
        return _DEFAULT_BASE_URL
    return url


class DispatcharrIntegrator:
    """Mount-side integration: naming via the shared core + live VOD data."""

    def is_available(self) -> bool:
        return DJANGO_AVAILABLE

    # --- naming (delegates entirely to the shared core) --------------------------

    def parse(self, raw: str, year_field: Any = None) -> Dict[str, Any]:
        return _naming.parse_title(raw, year_field)

    @staticmethod
    def folder_name_from_fields(name, year=None, tmdb_id=None, imdb_id=None) -> str:
        return _naming.folder_name(name, year, tmdb_id, imdb_id)

    def movie_folder_name(self, movie) -> str:
        return _naming.folder_name(
            movie.name, getattr(movie, 'year', None),
            getattr(movie, 'tmdb_id', None), getattr(movie, 'imdb_id', None))

    def movie_filename(self, movie, relation, provider_label: str = '') -> str:
        return _naming.movie_filename(
            movie.name, getattr(movie, 'year', None),
            getattr(movie, 'tmdb_id', None), getattr(movie, 'imdb_id', None),
            relation, provider_label)

    def series_folder_name(self, series) -> str:
        return _naming.folder_name(
            series.name, getattr(series, 'year', None),
            getattr(series, 'tmdb_id', None), getattr(series, 'imdb_id', None))

    @staticmethod
    def season_dir_name(season_number: int) -> str:
        return _naming.season_dir_name(season_number)

    def episode_filename(self, series, season_number: int, episode_number: int,
                         episode_name: str, relation, provider_label: str = '') -> str:
        return _naming.episode_filename(
            series.name, getattr(series, 'year', None),
            season_number, episode_number, episode_name, relation, provider_label)

    # --- proxy URLs (delegates to the shared playback path) ----------------------

    def get_proxy_url(self, content_type: str, uuid: str, stream_id: str) -> str:
        return _proxy_url(_get_dispatcharr_base_url(), content_type, uuid, stream_id)

    # --- episodes ----------------------------------------------------------------

    def get_series_episodes(self, series_uuid: str) -> List[Dict[str, Any]]:
        """Return episodes (with provider streams) for a series."""
        if not self.is_available():
            logger.warning("Django models not available")
            return []

        try:
            series = Series.objects.get(uuid=series_uuid)
        except Series.DoesNotExist:
            logger.warning("Series %s not found", series_uuid)
            return []

        episodes = list(series.episodes.all().order_by('season_number', 'episode_number'))
        episode_ids = [e.id for e in episodes]

        try:
            from django.db.models import F
        except ImportError:
            F = None

        relations_by_episode = defaultdict(list)
        if episode_ids and F is not None:
            # Size gate: hide episodes whose size Dispatcharr doesn't know yet, so
            # Plex never sees an unsized (un-probable) episode.
            ep_sized = ({"custom_properties__info__info__bitrate__gt": 0}
                        if os.environ.get("VOD2MLIB_REQUIRE_SIZE", "true").lower() == "true"
                        else {})
            relations = M3UEpisodeRelation.objects.filter(
                episode_id__in=episode_ids,
                series_relation__series=series,
                series_relation__category__m3u_relations__enabled=True,
                series_relation__category__m3u_relations__m3u_account=F("m3u_account"),
                m3u_account__is_active=True,
                **ep_sized,
            ).select_related('episode', 'm3u_account', 'series_relation',
                             'series_relation__category').distinct()
            for rel in relations:
                relations_by_episode[rel.episode_id].append(rel)

        result = []
        for episode in episodes:
            streams = []
            for rel in relations_by_episode.get(episode.id, []):
                stream_url = self.get_proxy_url("episode", str(episode.uuid), rel.stream_id)
                streams.append({
                    "stream_id": rel.stream_id,
                    "account_name": rel.m3u_account.name if rel.m3u_account else "Unknown",
                    "stream_url": stream_url,
                    "extension": rel.container_extension or "mkv",
                    "size": size_from_metadata(rel.custom_properties, episode.duration_secs),
                    "relation": rel,
                })
            result.append({
                "uuid": str(episode.uuid),
                "name": episode.name,
                "season_number": episode.season_number,
                "episode_number": episode.episode_number,
                "air_date": episode.air_date,
                "streams": streams,
            })
        return result


# Typical streaming bitrate (~2 Mbps). Used only as a fallback when the real
# Content-Length is unknown; the native proxy reports the true size on read.
_BYTES_PER_SEC = 2_000_000 // 8


def estimate_size(duration_secs: Optional[int]) -> int:
    if duration_secs and duration_secs > 0:
        return int(duration_secs) * _BYTES_PER_SEC
    return 2 * 1024 * 1024 * 1024  # 2 GiB fallback so clients never see a 0-byte file


def _provider_info_block(custom_properties) -> dict:
    """The provider 'info' block carrying overall bitrate + duration, normalising the
    two Dispatcharr shapes: movies store it at ``detailed_info`` (fetched on demand),
    episodes at ``info.info`` (populated during series hydration)."""
    cp = custom_properties or {}
    if not isinstance(cp, dict):
        try:
            cp = json.loads(cp)
        except (ValueError, TypeError):
            return {}
    di = cp.get('detailed_info')
    if isinstance(di, dict) and ('bitrate' in di or 'video' in di):
        return di
    inner = cp.get('info')
    inner = inner.get('info') if isinstance(inner, dict) else None
    return inner if isinstance(inner, dict) else {}


def size_from_bitrate(custom_properties, duration_secs: Optional[int] = None) -> Optional[int]:
    """Exact-ish size from Dispatcharr's stored provider metadata, or None if absent.

    Uses the *overall* bitrate (kbps) * duration — the whole-file rate. Returns None
    when there's no usable bitrate so the caller can fall through (an undersized
    estimate truncates the file and makes it unplayable)."""
    info = _provider_info_block(custom_properties)
    if not info:
        return None
    try:
        br = int(info.get('bitrate') or 0)
        dur = int(info.get('duration_secs') or duration_secs or 0)
    except (ValueError, TypeError):
        br = dur = 0
    if 100 <= br <= 200_000 and dur > 0:   # sane kbps; *1000/8 -> bytes
        return br * 125 * dur
    return None


def size_from_metadata(custom_properties, duration_secs: Optional[int] = None) -> int:
    """Bitrate-derived exact size if available, else the duration estimate."""
    return size_from_bitrate(custom_properties, duration_secs) or estimate_size(duration_secs)


# --- accurate size probing ------------------------------------------------------
_PROBE_ENABLED = os.environ.get("VOD2MLIB_PROBE_SIZE", "false").lower() == "true"
_size_cache: Dict[str, int] = {}
_size_cache_lock = threading.Lock()
_probe_sem = threading.Semaphore(int(os.environ.get("VOD2MLIB_PROBE_CONCURRENCY", "4")))


def probe_real_size(content_type: str, uuid: str, stream_id: str,
                    timeout: float = 10.0) -> Optional[int]:
    """Return the true byte size of a VOD item via the native proxy, or None."""
    if not _PROBE_ENABLED:
        return None
    key = "%s:%s" % (content_type, stream_id)
    with _size_cache_lock:
        if key in _size_cache:
            return _size_cache[key]
    url = _proxy_url(_get_dispatcharr_base_url(), content_type, uuid, stream_id)
    size = None
    try:
        with _probe_sem:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-0"}, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                cr = resp.headers.get("Content-Range")
                if cr and "/" in cr:
                    total = cr.rsplit("/", 1)[1].strip()
                    if total.isdigit():
                        size = int(total)
                if size is None:
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        size = int(cl)
    except Exception as e:
        logger.debug("size probe failed for stream_id=%s: %s", stream_id, e)
    if size:
        with _size_cache_lock:
            _size_cache[key] = size
    return size
