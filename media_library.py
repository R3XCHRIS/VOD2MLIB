"""Provider-independent identities and a bounded, paginated Emby adapter."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Identity:
    kind: str
    title: str
    year: object = None
    tmdb: str = ""
    imdb: str = ""


@dataclass
class OwnedMedia:
    identity: Identity
    episodes: set = field(default_factory=set)


class Snapshot:
    def __init__(self, media, clean_title=lambda s: s):
        self.media = media
        self.clean_title = clean_title
        self.ids = {}
        self.text = None
        for item in media:
            for provider in ("tmdb", "imdb"):
                value = getattr(item.identity, provider)
                if value:
                    self.ids.setdefault(
                        (item.identity.kind, provider, value), []
                    ).append(item)

    def key(self, identity):
        return (
            identity.kind,
            self.clean_title(identity.title).strip().casefold(),
            str(identity.year),
        )

    @staticmethod
    def compatible(a, b):
        return not any(
            getattr(a, p) and getattr(b, p) and getattr(a, p) != getattr(b, p)
            for p in ("tmdb", "imdb")
        )

    def matches(self, identity):
        found = []
        for provider in ("tmdb", "imdb"):
            value = getattr(identity, provider)
            if value:
                found.extend(self.ids.get((identity.kind, provider, value), []))
        matches = [x for x in found if self.compatible(identity, x.identity)]
        if matches:
            return matches
        if self.text is None:
            text = {}
            for item in self.media:
                if item.identity.year:
                    text.setdefault(self.key(item.identity), []).append(item)
            self.text = text
        if not identity.year:
            return []
        return [
            x
            for x in self.text.get(self.key(identity), [])
            if self.compatible(identity, x.identity)
            and not any(
                getattr(identity, p) and getattr(x.identity, p)
                for p in ("tmdb", "imdb")
            )
        ]

    def owns(self, identity, position=None, tv_mode="show"):
        matches = self.matches(identity)
        if identity.kind == "movie":
            return bool(matches)
        if tv_mode == "show":
            return bool(matches)
        return position is not None and any(position in x.episodes for x in matches)


class LibrarySelectionError(ValueError):
    """Invalid explicit selection, rather than a transient server failure."""


def resolve_library_ids(libraries, selections):
    """Resolve names afresh; reject missing or ambiguous names before file changes."""
    if not selections:
        raise LibrarySelectionError("Enter at least one library name or ID")
    by_id, by_name = {}, {}
    for library in libraries:
        identifier = str(library["Id"])
        by_id[identifier] = identifier
        by_name.setdefault(library["Name"].strip().casefold(), set()).add(identifier)
    resolved = []
    for selection in selections:
        selection = selection.strip()
        if not selection:
            raise LibrarySelectionError("Enter at least one library name or ID")
        if selection in by_id:
            identifier = by_id[selection]
        else:
            matches = by_name.get(selection.casefold(), set())
            if len(matches) != 1:
                reason = "ambiguous; use its ID" if matches else "not found"
                raise LibrarySelectionError(f"Library '{selection}' is {reason}")
            identifier = next(iter(matches))
        if identifier not in resolved:
            resolved.append(identifier)
    return resolved


class MediaLibraryAdapter(ABC):
    @abstractmethod
    def list_libraries(self):
        pass

    @abstractmethod
    def get_snapshot(self, library_ids, media_types):
        pass


class EmbyAdapter(MediaLibraryAdapter):
    PAGE_SIZE = 20000  # Bounded bulk lists; no playback/source detail payloads.

    def __init__(self, url, token):
        if (
            urlparse(url).scheme not in ("http", "https")
            or not urlparse(url).netloc
            or not token
        ):
            raise ValueError("Configure an Emby URL and API key")
        self.url, self.token = url.rstrip("/"), token

    def _get(self, path, params):
        request = Request(
            self.url + "/" + path + "?" + urlencode(params),
            headers={"X-Emby-Token": self.token, "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception:
            # Do not expose tokens or connection URLs in errors.
            raise RuntimeError(
                "Emby request failed; check connection and credentials"
            ) from None

    def _items(self, params):
        start, expected = 0, None
        seen = set()
        while True:
            query = dict(
                params,
                StartIndex=start,
                Limit=self.PAGE_SIZE,
                EnableImages="false",
                EnableUserData="false",
                EnableTotalRecordCount="true" if expected is None else "false",
            )
            page = self._get("Items", query)
            items = page.get("Items")
            if not isinstance(items, list):
                raise ValueError("Invalid Emby page")
            if expected is None:
                expected = page.get("TotalRecordCount")
                if type(expected) is not int or expected < 0:
                    raise ValueError("Invalid Emby item count")
            for item in items:
                if item["Id"] in seen:
                    raise ValueError("Repeated Emby page")
                seen.add(item["Id"])
                yield item
            start += len(items)
            callback = getattr(self, "progress", None)
            if callback:
                callback(f"Emby snapshot: {start:,} of {expected:,} items fetched")
            if start == expected:
                break
            if not items or start > expected:
                raise ValueError("Incomplete Emby snapshot")
        # Recount only once at the end instead of re-running COUNT for every
        # page. No matching/deletion can use the snapshot until this passes.
        check = self._get(
            "Items",
            dict(
                params,
                StartIndex=0,
                Limit=0,
                EnableImages="false",
                EnableUserData="false",
                EnableTotalRecordCount="true",
            ),
        )
        if check.get("Items") != [] or check.get("TotalRecordCount") != expected:
            raise ValueError("Emby catalogue changed during snapshot")

    def list_libraries(self):
        return self._get("Library/MediaFolders", {})["Items"]

    @staticmethod
    def identity(item, kind):
        ids = {
            k.lower(): str(v).strip().lower()
            for k, v in (item.get("ProviderIds") or {}).items()
            if v
        }
        return Identity(
            kind,
            item.get("Name") or "",
            item.get("ProductionYear"),
            ids.get("tmdb", ""),
            ids.get("imdb", ""),
        )

    @staticmethod
    def real(item):
        if item.get("IsVirtualItem") or item.get("LocationType") == "Virtual":
            return False
        sources = item.get("MediaSources") or [item]
        return any(
            s.get("Path")
            and not s["Path"].lower().split("?")[0].endswith(".strm")
            and "://" not in s["Path"]
            and s.get("Protocol", "File").lower() == "file"
            and not s.get("IsRemote", False)
            and s.get("Container", "").lower() != "strm"
            for s in sources
        )

    def get_snapshot(self, library_ids, media_types):
        if not library_ids or any(not str(library).strip() for library in library_ids):
            raise ValueError("At least one explicit Emby library ID is required")
        movies, series, episodes = [], {}, {}
        for library in dict.fromkeys(library_ids):
            params = {
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series,Episode",
                "Fields": "ProviderIds,Path",
                "GroupItemsIntoCollections": "false",
                "CollapseBoxSetItems": "false",
                "IsMissing": "false",
            }
            params["ParentId"] = library
            for item in self._items(params):
                kind = item.get("Type")
                if kind == "Series":
                    series[item["Id"]] = self.identity(item, "series")
                elif kind == "Movie" and "movie" in media_types and self.real(item):
                    movies.append(OwnedMedia(self.identity(item, "movie")))
                elif kind == "Episode" and self.real(item) and item.get("SeriesId"):
                    positions = episodes.setdefault(item["SeriesId"], set())
                    season, first = (
                        item.get("ParentIndexNumber"),
                        item.get("IndexNumber"),
                    )
                    last = item.get("IndexNumberEnd", first)
                    if (
                        isinstance(season, int)
                        and isinstance(first, int)
                        and isinstance(last, int)
                        and 0 <= last - first < 1000
                    ):
                        positions.update((season, n) for n in range(first, last + 1))
        return Snapshot(
            movies
            + [
                OwnedMedia(series[s], p)
                for s, p in episodes.items()
                if s in series and "series" in media_types
            ]
        )


def create_adapter(settings):
    if settings.get("media_server", "emby") != "emby":
        raise ValueError("Unsupported media server")
    return EmbyAdapter(
        settings.get("media_server_url", ""), settings.get("media_server_token", "")
    )
