"""Virtual filesystem over Dispatcharr VOD, backed by live DB queries.

Layout (siblings ``All`` + per-category, per design):
    /Movies/{All|Category}/{Title (Year) {ids}}/{Title (Year) {ids} - provider - sid.ext}
    /Series/{All|Category}/{Title (Year) {ids}}/Season NN/{Title (Year) - SnnEmm - ep - sid.ext}

Files are keyed on the provider ``stream_id`` (unique per M3U account), so a file
path resolves to an exact provider stream without depending on lossy title
re-parsing. Folder -> object lookups resolve by tmdb/imdb id when the folder
carries one, falling back to a selective title match for id-less folders.
"""

import os
import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

try:
    from .integration import (
        DispatcharrIntegrator, parse_title, estimate_size, probe_real_size,
        size_from_metadata, size_from_bitrate,
    )
except ImportError:
    from integration import (
        DispatcharrIntegrator, parse_title, estimate_size, probe_real_size,
        size_from_metadata, size_from_bitrate,
    )

from vodlib.config import ENV_REQUIRE_SIZE

logger = logging.getLogger(__name__)

_VIDEO_CT = "video/x-matroska"
_TMDB_RE = re.compile(r'\{tmdb-(\w+)\}')
_IMDB_RE = re.compile(r'\{imdb-(tt\w+)\}')
_STREAMID_RE = re.compile(r'-\s*([0-9]+)\.[A-Za-z0-9]+$')   # trailing ' - <sid>.ext'
_SEASON_RE = re.compile(r'^Season\s+(\d{1,3})$', re.IGNORECASE)

# Eventual-consistency gate: only surface titles whose real size Dispatcharr already
# knows (stored bitrate). Unsized titles stay hidden so Plex never sees — and never
# probes the provider for — a file with an unknown size. Dispatcharr backfills sizes
# over time (movies via refresh_movie_advanced_data; episodes during series
# hydration), and titles appear as their size lands. Set VOD2MLIB_REQUIRE_SIZE=false to
# show everything (then enable VOD2MLIB_PROBE_SIZE so playback still gets a real size).
_REQUIRE_SIZE = os.environ.get(ENV_REQUIRE_SIZE, "true").lower() == "true"
# JSON paths to the stored overall bitrate (two Dispatcharr shapes).
_MOVIE_SIZED = {"custom_properties__detailed_info__bitrate__gt": 0}
_SERIES_HAS_SIZED_EP = {
    "series__episodes__m3u_relations__custom_properties__info__info__bitrate__gt": 0}
_EPISODE_SIZED = {"custom_properties__info__info__bitrate__gt": 0}


# Only enabled categories whose account matches the relation's account, and only
# while that account is active. Without the is_active gate a deactivated provider
# whose category relations are still enabled would keep surfacing files that 302
# to a dead proxy (Dispatcharr stops serving inactive accounts).
def _enabled(**extra):
    from django.db.models import F
    base = dict(
        category__m3u_relations__enabled=True,
        category__m3u_relations__m3u_account=F("m3u_account"),
        m3u_account__is_active=True,
    )
    base.update(extra)
    return base


class NodeType(Enum):
    DIRECTORY = "directory"
    FILE = "file"


@dataclass
class FSNode:
    name: str
    node_type: NodeType
    metadata: dict = field(default_factory=dict)

    def is_directory(self) -> bool:
        return self.node_type == NodeType.DIRECTORY

    def is_file(self) -> bool:
        return self.node_type == NodeType.FILE

    def get_file_size(self) -> int:
        return self.metadata.get("size", 0)

    def get_content_type(self) -> str:
        return self.metadata.get("content_type", "application/octet-stream")


class DirectoryNode(FSNode):
    def __init__(self, name: str, **meta):
        super().__init__(name, NodeType.DIRECTORY)
        self.metadata.update(meta)


class FileNode(FSNode):
    def __init__(self, name: str, stream_url: str, size: int = 0,
                 content_type: str = _VIDEO_CT):
        super().__init__(name, NodeType.FILE)
        self.metadata.update(stream_url=stream_url, size=size, content_type=content_type)


def _parse_folder(folder_name: str):
    """folder 'Title (Year) {tmdb-N} {imdb-ttX}' -> (title, year, tmdb, imdb)."""
    tmdb = _TMDB_RE.search(folder_name)
    imdb = _IMDB_RE.search(folder_name)
    base = re.sub(r'\s*\{[^}]*\}', '', folder_name).strip()
    ym = re.search(r'\((\d{4})\)\s*$', base)
    year = int(ym.group(1)) if ym else None
    title = re.sub(r'\s*\(\d{4}\)\s*$', '', base).strip()
    return title, year, (tmdb.group(1) if tmdb else None), (imdb.group(1) if imdb else None)


class VirtualTree:
    """Live VOD tree. No manifest caching; all data comes from the DB on demand."""

    def __init__(self):
        self._integrator = DispatcharrIntegrator()

    @property
    def available(self) -> bool:
        return self._integrator.is_available()

    # --- listings ---------------------------------------------------------------

    def categories(self, content_type: str = 'movie') -> List[str]:
        try:
            from apps.vod.models import VODCategory
        except ImportError:
            return []
        return list(
            VODCategory.objects.filter(
                category_type=content_type, m3u_relations__enabled=True,
            ).values_list('name', flat=True).distinct().order_by('name')
        )

    def movies_stream(self, category: Optional[str] = None):
        """Yield movie folder names (one per movie), streamed with bounded memory.

        Ordered by ``movie_id`` (not folder name) so the queryset can be walked with
        a server-side cursor via ``.iterator()`` and ``.values()`` — no model objects,
        no full materialisation. Consecutive rows share a movie_id (multiple provider
        relations), so we emit each movie once. Memory is O(chunk) at any size; this is
        the path that survives a 100k-title ``/Movies/All``. Folder order is not
        guaranteed (rclone/Plex enumerate the whole dir regardless of order)."""
        try:
            from apps.vod.models import M3UMovieRelation
        except ImportError:
            return
        sized = _MOVIE_SIZED if _REQUIRE_SIZE else {}
        qs = (M3UMovieRelation.objects.filter(**_enabled(), **sized)
              .values('movie_id', 'movie__name', 'movie__year',
                      'movie__tmdb_id', 'movie__imdb_id')
              .order_by('movie_id'))
        if category:
            qs = qs.filter(category__name=category)
        last = object()
        for row in qs.iterator(chunk_size=2000):
            if row['movie_id'] == last:
                continue
            last = row['movie_id']
            yield self._integrator.folder_name_from_fields(
                row['movie__name'], row['movie__year'],
                row['movie__tmdb_id'], row['movie__imdb_id'])

    def movie_files(self, folder_name: str, category: Optional[str] = None) -> List[dict]:
        """Files (one per provider stream) inside a movie folder."""
        movie = self._lookup_movie(folder_name, category)
        if not movie:
            return []
        try:
            from apps.vod.models import M3UMovieRelation
        except ImportError:
            return []
        sized = _MOVIE_SIZED if _REQUIRE_SIZE else {}
        qs = M3UMovieRelation.objects.filter(
            **_enabled(movie=movie), **sized).select_related('m3u_account', 'category')
        if category:
            qs = qs.filter(category__name=category)
        rels = list(qs)               # materialise once; we count and iterate it
        multi = len(rels) > 1
        out = []
        for rel in rels:
            provider = (rel.m3u_account.name[:24] if (multi and rel.m3u_account) else '')
            fn = self._integrator.movie_filename(movie, rel, provider)
            url = self._integrator.get_proxy_url("movie", str(movie.uuid), rel.stream_id)
            out.append({'filename': fn,
                        'size': size_from_metadata(rel.custom_properties, movie.duration_secs),
                        'stream_url': url})
        out.sort(key=lambda e: e['filename'].lower())
        return out

    def series_stream(self, category: Optional[str] = None):
        """Yield series folder names (one per series), streamed — see movies_stream."""
        try:
            from apps.vod.models import M3USeriesRelation
        except ImportError:
            return
        sized = _SERIES_HAS_SIZED_EP if _REQUIRE_SIZE else {}
        qs = (M3USeriesRelation.objects.filter(**_enabled(), **sized)
              .values('series_id', 'series__name', 'series__year',
                      'series__tmdb_id', 'series__imdb_id')
              .order_by('series_id'))
        if category:
            qs = qs.filter(category__name=category)
        last = object()
        for row in qs.iterator(chunk_size=2000):
            if row['series_id'] == last:
                continue
            last = row['series_id']
            yield self._integrator.folder_name_from_fields(
                row['series__name'], row['series__year'],
                row['series__tmdb_id'], row['series__imdb_id'])

    def _series_episodes(self, show_name: str, category: Optional[str]):
        series = self._lookup_series(show_name, category)
        if not series:
            return None, []
        return series, self._integrator.get_series_episodes(str(series.uuid))

    def seasons(self, show_name: str, category: Optional[str] = None) -> List[str]:
        _, episodes = self._series_episodes(show_name, category)
        nums = sorted({e['season_number'] for e in episodes
                       if e['season_number'] is not None and e.get('streams')})
        return [self._integrator.season_dir_name(n) for n in nums]

    def episodes(self, show_name: str, season_name: str,
                 category: Optional[str] = None) -> List[dict]:
        m = _SEASON_RE.match(season_name or '')
        if not m:
            return []
        want_season = int(m.group(1))
        series, episodes = self._series_episodes(show_name, category)
        if not series:
            return []
        out = []
        for ep in episodes:
            if ep['season_number'] != want_season:
                continue
            for st in ep.get('streams', []):
                rel = st['relation']
                provider = (st['account_name'][:24]
                            if len(ep['streams']) > 1 and st['account_name'] else '')
                fn = self._integrator.episode_filename(
                    series, ep['season_number'], ep['episode_number'], ep['name'],
                    rel, provider)
                out.append({'filename': fn, 'size': st['size'],
                            'stream_url': st['stream_url']})
        out.sort(key=lambda e: e['filename'].lower())
        return out

    # --- object lookups ---------------------------------------------------------

    def _lookup_movie(self, folder_name: str, category: Optional[str]):
        try:
            from apps.vod.models import Movie
        except ImportError:
            return None
        title, year, tmdb, imdb = _parse_folder(folder_name)
        if tmdb:
            mv = Movie.objects.filter(tmdb_id=tmdb).first()
            if mv:
                return mv
        if imdb:
            mv = Movie.objects.filter(imdb_id=imdb).first()
            if mv:
                return mv
        return self._match_by_title(Movie, title, year)

    def _lookup_series(self, folder_name: str, category: Optional[str]):
        try:
            from apps.vod.models import Series
        except ImportError:
            return None
        title, year, tmdb, imdb = _parse_folder(folder_name)
        if tmdb:
            s = Series.objects.filter(tmdb_id=tmdb).first()
            if s:
                return s
        return self._match_by_title(Series, title, year)

    @staticmethod
    def _match_by_title(model, title: str, year: Optional[int]):
        """Best-effort title match for items lacking external IDs.

        Filters candidates by the most *distinctive* word in the title (longest, not
        the first) — searching by "The" matches thousands and the real title can fall
        outside the candidate cap, so descent into a no-tmdb folder would 404. The
        provider name carries the cleaned title's words even amid junk, so the longest
        word is a reliable, selective probe.
        """
        if not title:
            return None
        words = [w for w in re.findall(r"[A-Za-z0-9]+", title) if len(w) >= 3]
        search = max(words, key=len) if words else title[:12]
        candidates = list(model.objects.filter(name__icontains=search)[:400])
        for obj in candidates:
            p = parse_title(obj.name, getattr(obj, 'year', None))
            if p['title'].lower() == title.lower() and (year is None or p['year'] == year):
                return obj
        # looser: title match only
        for obj in candidates:
            if parse_title(obj.name, getattr(obj, 'year', None))['title'].lower() == title.lower():
                return obj
        return None

    # --- path resolution (single node, used to serve files / 404) ---------------

    def resolve_path_with_db(self, path: str) -> Optional[FSNode]:
        comps = [c for c in (path or '').split('/') if c]
        if not comps:
            return DirectoryNode("")
        top = comps[0]
        if top not in ("Movies", "Series"):
            return None
        if len(comps) == 1:
            return DirectoryNode(top)
        # comps: [Movies|Series, All|Category, ...]
        if top == "Movies":
            return self._resolve_movie(comps)
        return self._resolve_series(comps)

    def _resolve_movie(self, comps: List[str]) -> Optional[FSNode]:
        category = None if comps[1] == "All" else comps[1]
        if len(comps) == 2:                      # /Movies/All
            return DirectoryNode(comps[1])
        if len(comps) == 3:                      # /Movies/All/{folder}
            if self._lookup_movie(comps[2], category):
                return DirectoryNode(comps[2])
            return None
        if len(comps) == 4:                      # /Movies/All/{folder}/{file}
            return self._resolve_movie_file("movie", comps[3])
        return None

    def _resolve_series(self, comps: List[str]) -> Optional[FSNode]:
        category = None if comps[1] == "All" else comps[1]
        if len(comps) == 2:                      # /Series/All
            return DirectoryNode(comps[1])
        if len(comps) == 3:                      # /Series/All/{show}
            if self._lookup_series(comps[2], category):
                return DirectoryNode(comps[2])
            return None
        if len(comps) == 4:                      # /Series/All/{show}/Season NN
            if _SEASON_RE.match(comps[3]) and self._lookup_series(comps[2], category):
                return DirectoryNode(comps[3])
            return None
        if len(comps) == 5:                      # /Series/All/{show}/Season NN/{file}
            return self._resolve_movie_file("episode", comps[4])
        return None

    def _resolve_movie_file(self, kind: str, filename: str) -> Optional[FileNode]:
        m = _STREAMID_RE.search(filename)
        if not m:
            return None
        stream_id = m.group(1)
        try:
            if kind == "movie":
                from apps.vod.models import M3UMovieRelation
                sized = _MOVIE_SIZED if _REQUIRE_SIZE else {}
                rel = M3UMovieRelation.objects.filter(
                    stream_id=stream_id, **_enabled(), **sized
                ).select_related('movie').first()
                if not rel:
                    return None
                uuid = str(rel.movie.uuid)
                # Stored bitrate (free, exact) -> probe (only if VOD2MLIB_PROBE_SIZE) ->
                # estimate. With the size gate on, bitrate is always present here.
                size = (size_from_bitrate(rel.custom_properties, rel.movie.duration_secs)
                        or probe_real_size("movie", uuid, stream_id)
                        or estimate_size(rel.movie.duration_secs))
                url = self._integrator.get_proxy_url("movie", uuid, stream_id)
                return FileNode(filename, url, size)
            else:
                from apps.vod.models import M3UEpisodeRelation
                sized = _EPISODE_SIZED if _REQUIRE_SIZE else {}
                rel = M3UEpisodeRelation.objects.filter(
                    stream_id=stream_id,
                    series_relation__category__m3u_relations__enabled=True,
                    m3u_account__is_active=True,
                    **sized,
                ).select_related('episode').first()
                if not rel:
                    return None
                uuid = str(rel.episode.uuid)
                size = (size_from_bitrate(rel.custom_properties, rel.episode.duration_secs)
                        or probe_real_size("episode", uuid, stream_id)
                        or estimate_size(rel.episode.duration_secs))
                url = self._integrator.get_proxy_url("episode", uuid, stream_id)
                return FileNode(filename, url, size)
        except Exception:
            logger.debug("file resolve failed for %s", filename, exc_info=True)
            return None
