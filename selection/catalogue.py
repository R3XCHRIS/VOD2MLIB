"""Read-only view of the VOD catalogue for the selection page.

Two implementations share one interface:
  * DjangoCatalogue: the real thing, inside Dispatcharr (ORM).
  * FakeCatalogue: in-memory, for tests and the local dev server.

`kind` is "movie" or "series". A copy is identified by (account_id, copy_id):
the relation's stream_id for a movie, its external_series_id for a series.

Items returned to the page are plain dicts; resolve_copy() returns the
(title, relation) objects the existing Plugin helpers expect, and
series_episodes() the (episode, stream_id, title) triples to write for a
series copy.
"""
import functools
from types import SimpleNamespace

from .copyinfo import copy_info, rank

KINDS = ("movie", "series")

# Year filter buckets: "2020s", "1990s", ..., "before-1950", "none". Years
# before 1900 are provider junk (0, 1) and count as no year.
YEAR_MIN = 1900
YEAR_OLD = 1950


def decade_key(year):
    if not year or year < YEAR_MIN:
        return "none"
    if year < YEAR_OLD:
        return "before-1950"
    return f"{year // 10 * 10}s"


def valid_decade(key):
    return key in ("none", "before-1950") or (
        len(key) == 5 and key.endswith("0s") and key[:4].isdigit() and int(key[:4]) >= YEAR_OLD)


def _decades(years):
    """[{"key", "titles"}] from an iterable of (year, count), newest first,
    then before-1950, then none."""
    counts = {}
    for year, n in years:
        key = decade_key(year)
        counts[key] = counts.get(key, 0) + n
    order = lambda k: (0, -int(k[:4])) if k[0].isdigit() else ((1, 0) if k == "before-1950" else (2, 0))
    return [{"key": k, "titles": counts[k]} for k in sorted(counts, key=order)]


def _copy_id(kind, rel):
    return str(rel.stream_id if kind == "movie" else rel.external_series_id)


def copy_key(kind, rel):
    """A copy's identity: (account id, stream id / external series id)."""
    return rel.m3u_account_id, _copy_id(kind, rel)


def _copy_dict(kind, rel, episode_props=None):
    """`episode_props`: custom_properties of one of a series copy's episodes,
    where its video data lives (None if its episodes were never fetched)."""
    category = rel.category.name if rel.category else ""
    props = getattr(rel, "custom_properties", None) or {}
    name = (props.get("basic_data") or {}).get("name") or ""
    if kind == "movie":
        media = props.get("detailed_info")
    else:
        media = ((episode_props or {}).get("info") or {}).get("info")
    return {
        "account_id": rel.m3u_account_id,
        "account_name": getattr(rel.m3u_account, "name", "") or f"Account {rel.m3u_account_id}",
        "stream_id": _copy_id(kind, rel),
        "category": category,
        "ext": getattr(rel, "container_extension", "") or "",
        "info": copy_info(name, category, media),
    }


def _title_dict(plugin, title, copies):
    return {
        "uuid": str(title.uuid),
        "name": title.name or "",
        "year": title.year,
        "poster": plugin._logo_url(title),
        "copies": rank(copies),  # best first, so the page's default copy is the best one
        "added_at": _timestamp(getattr(title, "created_at", None)),
    }


def _timestamp(value):
    """Epoch seconds from a Django datetime (or a plain number in the fake)."""
    if value is None:
        return None
    return value.timestamp() if hasattr(value, "timestamp") else float(value)


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")


def _episode_title(rel):
    """The copy's own episode title. Copies share one Episode row whose name
    is whichever copy was fetched last (a German title for the English
    copy, say), but each relation keeps its provider's title."""
    info = (getattr(rel, "custom_properties", None) or {}).get("info") or {}
    return info.get("title") or rel.episode.name


def _dedupe_episodes(episode_rels):
    """One Episode can be reached by several relations of the same copy;
    keep the first (callers sort by id for a stable winner), as classic
    mode does."""
    seen, out = set(), []
    for rel in episode_rels:
        if rel.episode.uuid in seen:
            continue
        seen.add(rel.episode.uuid)
        out.append((rel.episode, rel.stream_id, _episode_title(rel)))
    return out


def _with_db_connection(fn):
    """Page-server threads live outside Django's request cycle, so stale
    connections are not recycled for us."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from django.db import close_old_connections
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            close_old_connections()
    return wrapper


def _django_models(kind):
    from apps.vod import models
    if kind == "movie":
        return models.Movie, models.M3UMovieRelation
    return models.Series, models.M3USeriesRelation


class DjangoCatalogue:
    def __init__(self, plugin):
        self.plugin = plugin

    def _relations(self, kind, settings, apply_filters=True):
        _, rel_model = _django_models(kind)
        qs = rel_model.objects.filter(m3u_account__is_active=True)
        if apply_filters:
            qs = self.plugin._apply_category_filter(qs, (settings.get("category_filter") or "").strip())
            qs = self.plugin._apply_category_exclude(qs, (settings.get("category_exclude") or "").strip())
        return qs

    @_with_db_connection
    def accounts(self, kind, settings):
        """Active accounts with at least one visible title of this kind:
        [{"id", "name", "titles"}], by account id."""
        _check_kind(kind)
        from django.db.models import Count
        fk = f"{kind}_id"
        rows = (self._relations(kind, settings)
                .values("m3u_account_id", "m3u_account__name")
                .annotate(titles=Count(fk, distinct=True))
                .order_by("m3u_account_id"))
        return [{"id": r["m3u_account_id"], "name": r["m3u_account__name"] or f"Account {r['m3u_account_id']}",
                 "titles": r["titles"]} for r in rows]

    @_with_db_connection
    def facets(self, kind, settings, account_id=None):
        """Category and decade choices for the page, with title counts,
        within the provider filter: {"categories": [{"name", "titles"}] by
        name, "decades": [{"key", "titles"}]}."""
        _check_kind(kind)
        from django.db.models import Count
        model, _ = _django_models(kind)
        fk = f"{kind}_id"
        rels = self._relations(kind, settings)
        if account_id is not None:
            rels = rels.filter(m3u_account_id=account_id)
        cats = (rels.exclude(category__isnull=True)
                .values("category__name")
                .annotate(titles=Count(fk, distinct=True))
                .order_by("category__name"))
        years = (model.objects.filter(id__in=rels.values(fk))
                 .values("year").annotate(n=Count("id")).values_list("year", "n"))
        return {"categories": [{"name": r["category__name"], "titles": r["titles"]} for r in cats
                               if r["category__name"]],
                "decades": _decades(years)}

    @_with_db_connection
    def list_titles(self, kind, settings, q="", include_uuids=None, exclude_uuids=None, offset=0, limit=50,
                    added_after=None, account_id=None, category=None, decade=None):
        _check_kind(kind)
        from django.db.models import Q
        model, _ = _django_models(kind)
        fk = f"{kind}_id"
        # Selected titles always show under the "selected" filter, even if a
        # category filter/exclude would now hide them: settings never deselect.
        rels = self._relations(kind, settings, apply_filters=include_uuids is None)
        if include_uuids is not None:
            # Exactly these titles, copies or not: one that lost every copy
            # ('no_copy') still shows, so it can be seen and unselected.
            titles = model.objects.filter(uuid__in=list(include_uuids)).select_related("logo")
        else:
            titles = model.objects.filter(id__in=rels.values(fk)).select_related("logo")
        if account_id is not None or category:  # one copy matching both; every copy is still listed
            matching = rels
            if account_id is not None:
                matching = matching.filter(m3u_account_id=account_id)
            if category:
                matching = matching.filter(category__name=category)
            titles = titles.filter(id__in=matching.values(fk))
        if decade == "none":
            titles = titles.filter(Q(year__isnull=True) | Q(year__lt=YEAR_MIN))
        elif decade == "before-1950":
            titles = titles.filter(year__gte=YEAR_MIN, year__lt=YEAR_OLD)
        elif decade:
            start = int(decade[:4])
            titles = titles.filter(year__gte=start, year__lt=start + 10)
        if q:
            titles = titles.filter(name__icontains=q)
        if include_uuids is not None:
            titles = titles.filter(uuid__in=list(include_uuids))
        if exclude_uuids:
            titles = titles.exclude(uuid__in=list(exclude_uuids))
        if added_after is not None:
            from datetime import datetime, timezone
            titles = titles.filter(created_at__gt=datetime.fromtimestamp(added_after, tz=timezone.utc))
        total = titles.count()
        page = list(titles.order_by("name", "id")[offset:offset + limit])

        copies = {}
        page_rels = list(
            rels.filter(**{f"{fk}__in": [t.id for t in page]})
            .select_related("m3u_account", "category")
            .order_by("m3u_account_id", "id")
        )
        samples = self._episode_samples(page_rels) if kind == "series" else {}
        for rel in page_rels:
            copies.setdefault(getattr(rel, fk), []).append(_copy_dict(kind, rel, samples.get(rel.id)))
        return {
            "total": total,
            "items": [_title_dict(self.plugin, t, copies.get(t.id, [])) for t in page],
        }

    @staticmethod
    def _episode_samples(series_rels):
        """{series relation id: custom_properties of its first episode}, in
        one query (DISTINCT ON, PostgreSQL)."""
        from apps.vod.models import M3UEpisodeRelation
        rows = (
            M3UEpisodeRelation.objects
            .filter(series_relation_id__in=[r.id for r in series_rels])
            .order_by("series_relation_id", "id")
            .distinct("series_relation_id")
            .values_list("series_relation_id", "custom_properties")
        )
        return dict(rows)

    @_with_db_connection
    def resolve_copy(self, kind, content_uuid, account_id, copy_id):
        _check_kind(kind)
        _, rel_model = _django_models(kind)
        id_field = "stream_id" if kind == "movie" else "external_series_id"
        rel = (
            rel_model.objects
            .select_related(kind, f"{kind}__logo", "category", "m3u_account")
            .filter(**{f"{kind}__uuid": content_uuid, "m3u_account_id": account_id,
                       id_field: str(copy_id), "m3u_account__is_active": True})
            .first()
        )
        return (getattr(rel, kind), rel) if rel else (None, None)

    @_with_db_connection
    def title_ids(self, kind, uuids):
        """{uuid: tmdb_id} for the given uuids that still exist."""
        _check_kind(kind)
        model, _ = _django_models(kind)
        rows = model.objects.filter(uuid__in=list(uuids)).values_list("uuid", "tmdb_id")
        return {str(u): (t or "") for u, t in rows}

    @_with_db_connection
    def find_by_copy(self, kind, account_id, copy_id):
        """The uuid of the title an active copy belongs to, or None."""
        _check_kind(kind)
        _, rel_model = _django_models(kind)
        id_field = "stream_id" if kind == "movie" else "external_series_id"
        found = (rel_model.objects
                 .filter(**{"m3u_account_id": account_id, id_field: str(copy_id), "m3u_account__is_active": True})
                 .values_list(f"{kind}__uuid", flat=True).first())
        return str(found) if found else None

    @_with_db_connection
    def find_by_tmdb(self, kind, tmdb_id):
        """Uuids of titles with this TMDB id (at most 2: more means ambiguous too)."""
        _check_kind(kind)
        model, _ = _django_models(kind)
        return [str(u) for u in model.objects.filter(tmdb_id=str(tmdb_id)).values_list("uuid", flat=True)[:2]]

    @_with_db_connection
    def title_copies(self, kind, content_uuid):
        """[(title, relation)] for every active copy of a title."""
        _check_kind(kind)
        _, rel_model = _django_models(kind)
        rels = (
            rel_model.objects
            .select_related(kind, "category", "m3u_account")
            .filter(**{f"{kind}__uuid": content_uuid, "m3u_account__is_active": True})
            .order_by("m3u_account_id", "id")
        )
        return [(getattr(rel, kind), rel) for rel in rels]

    @_with_db_connection
    def titles_with_copies(self, kind, content_uuids):
        """The subset of the given uuids that still have an active copy (one
        query; the mass-loss guard checks every applied title)."""
        _check_kind(kind)
        _, rel_model = _django_models(kind)
        found = (rel_model.objects
                 .filter(**{f"{kind}__uuid__in": list(content_uuids), "m3u_account__is_active": True})
                 .values_list(f"{kind}__uuid", flat=True)
                 .distinct())
        return {str(u) for u in found}

    @_with_db_connection
    def identify_strm(self, url_kind, content_uuid, stream_id):
        """What an existing .strm plays, from its proxy URL (see adopt.py):
        {"kind", "uuid", "name", "copy": (account_id, copy_id) | None}, or
        None when Dispatcharr doesn't know the content (any more)."""
        from apps.vod.models import Episode, M3UEpisodeRelation, M3UMovieRelation, Movie
        if url_kind == "movie":
            movie = Movie.objects.filter(uuid=content_uuid).first()
            if movie is None:
                return None
            rel = (M3UMovieRelation.objects.filter(movie=movie, stream_id=stream_id,
                                                   m3u_account__is_active=True).first()
                   if stream_id else None)
            return {"kind": "movie", "uuid": str(movie.uuid), "name": movie.name or "",
                    "copy": (rel.m3u_account_id, str(rel.stream_id)) if rel else None}
        rel = (M3UEpisodeRelation.objects.filter(episode__uuid=content_uuid, stream_id=stream_id)
               .select_related("series_relation", "series_relation__m3u_account", "episode__series").first()
               if stream_id else None)
        series = rel.episode.series if rel else getattr(
            Episode.objects.filter(uuid=content_uuid).select_related("series").first(), "series", None)
        if series is None:
            return None
        srel = rel.series_relation if rel else None
        copy_ = ((srel.m3u_account_id, str(srel.external_series_id))
                 if srel is not None and srel.m3u_account.is_active else None)
        return {"kind": "series", "uuid": str(series.uuid), "name": series.name or "", "copy": copy_}

    @_with_db_connection
    def series_episodes(self, series_rel):
        """Fetch the copy's episode list from the provider (one API call),
        then return that copy's episodes.

        Filtered by the episode relation's own series_relation, not by
        account + series as classic mode does: two copies of a series on the
        same account (e.g. an English and a German one) would otherwise mix,
        and a copy change would keep the other copy's streams."""
        from apps.vod.models import M3UEpisodeRelation
        from apps.vod.tasks import refresh_series_episodes
        refresh_series_episodes(
            account=series_rel.m3u_account,
            series=series_rel.series,
            external_series_id=series_rel.external_series_id,
        )
        rels = (
            M3UEpisodeRelation.objects
            .filter(series_relation=series_rel)
            .select_related("episode")
            .order_by("episode__season_number", "episode__episode_number", "id")
        )
        return _dedupe_episodes(rels)


class FakeCatalogue:
    """In-memory catalogue. `titles[kind]` are objects shaped like
    Dispatcharr's Movie / Series; `relations[kind]` like M3UMovieRelation /
    M3USeriesRelation; `episodes[relation id]` like M3UEpisodeRelation
    (see make_fake_catalogue)."""

    def __init__(self, plugin, titles, relations, episodes=None):
        self.plugin = plugin
        self.titles = titles
        self.relations = relations
        self.episodes = episodes or {}

    def _visible_rels(self, kind, settings, apply_filters=True):
        include = self.plugin._parse_category_filter(settings.get("category_filter"))
        exclude = self.plugin._parse_category_filter(settings.get("category_exclude"))
        out = []
        for rel in self.relations[kind]:
            if not rel.m3u_account.is_active:
                continue
            cat = rel.category.name if rel.category else ""
            if apply_filters:
                if include and not self.plugin._matches_category_prefixes(cat, include):
                    continue
                if exclude and self.plugin._matches_category_prefixes(cat, exclude):
                    continue
            out.append(rel)
        return out

    def accounts(self, kind, settings):
        _check_kind(kind)
        titles = {}
        for rel in self._visible_rels(kind, settings):
            titles.setdefault(rel.m3u_account_id, (rel.m3u_account, set()))[1].add(getattr(rel, kind).id)
        return [{"id": aid, "name": acc.name or f"Account {aid}", "titles": len(ids)}
                for aid, (acc, ids) in sorted(titles.items())]

    def facets(self, kind, settings, account_id=None):
        _check_kind(kind)
        cats, titles = {}, {}
        for rel in self._visible_rels(kind, settings):
            if account_id is not None and rel.m3u_account_id != account_id:
                continue
            title = getattr(rel, kind)
            titles[title.id] = title
            if rel.category and rel.category.name:
                cats.setdefault(rel.category.name, set()).add(title.id)
        return {"categories": [{"name": n, "titles": len(ids)} for n, ids in sorted(cats.items())],
                "decades": _decades((t.year, 1) for t in titles.values())}

    def list_titles(self, kind, settings, q="", include_uuids=None, exclude_uuids=None, offset=0, limit=50,
                    added_after=None, account_id=None, category=None, decade=None):
        _check_kind(kind)
        rels = self._visible_rels(kind, settings, apply_filters=include_uuids is None)
        by_title = {}
        for rel in sorted(rels, key=lambda r: (r.m3u_account_id, r.id)):
            by_title.setdefault(getattr(rel, kind).id, []).append(rel)
        inc = {str(u) for u in include_uuids} if include_uuids is not None else set()
        titles = [t for t in self.titles[kind] if t.id in by_title or str(t.uuid) in inc]
        if account_id is not None or category:
            def matches(r):
                return ((account_id is None or r.m3u_account_id == account_id)
                        and (not category or (r.category and r.category.name == category)))
            titles = [t for t in titles if any(matches(r) for r in by_title.get(t.id, []))]
        if decade:
            titles = [t for t in titles if decade_key(t.year) == decade]
        if q:
            titles = [t for t in titles if q.lower() in (t.name or "").lower()]
        if include_uuids is not None:
            titles = [t for t in titles if str(t.uuid) in inc]
        if exclude_uuids:
            exc = {str(u) for u in exclude_uuids}
            titles = [t for t in titles if str(t.uuid) not in exc]
        if added_after is not None:
            titles = [t for t in titles if (_timestamp(getattr(t, "created_at", None)) or 0) > added_after]
        titles.sort(key=lambda t: ((t.name or "").lower(), t.id))
        page = titles[offset:offset + limit]
        def sample(rel):
            eps = self.episodes.get(rel.id) if kind == "series" else None
            return eps[0].custom_properties if eps else None

        return {
            "total": len(titles),
            "items": [
                _title_dict(self.plugin, t, [_copy_dict(kind, r, sample(r)) for r in by_title.get(t.id, [])])
                for t in page
            ],
        }

    def resolve_copy(self, kind, content_uuid, account_id, copy_id):
        _check_kind(kind)
        for rel in self.relations[kind]:
            title = getattr(rel, kind)
            if (str(title.uuid) == str(content_uuid)
                    and rel.m3u_account_id == account_id
                    and _copy_id(kind, rel) == str(copy_id)
                    and rel.m3u_account.is_active):
                return title, rel
        return None, None

    def title_ids(self, kind, uuids):
        wanted = {str(u) for u in uuids}
        return {str(t.uuid): (t.tmdb_id or "") for t in self.titles[kind] if str(t.uuid) in wanted}

    def find_by_copy(self, kind, account_id, copy_id):
        for rel in self.relations[kind]:
            if (rel.m3u_account_id == account_id and _copy_id(kind, rel) == str(copy_id)
                    and rel.m3u_account.is_active):
                return str(getattr(rel, kind).uuid)
        return None

    def find_by_tmdb(self, kind, tmdb_id):
        return [str(t.uuid) for t in self.titles[kind] if (t.tmdb_id or "") == str(tmdb_id)][:2]

    def title_copies(self, kind, content_uuid):
        _check_kind(kind)
        return [(getattr(rel, kind), rel) for rel in self.relations[kind]
                if str(getattr(rel, kind).uuid) == str(content_uuid) and rel.m3u_account.is_active]

    def titles_with_copies(self, kind, content_uuids):
        _check_kind(kind)
        wanted = {str(u) for u in content_uuids}
        return {str(getattr(rel, kind).uuid) for rel in self.relations[kind]
                if rel.m3u_account.is_active and str(getattr(rel, kind).uuid) in wanted}

    def identify_strm(self, url_kind, content_uuid, stream_id):
        if url_kind == "movie":
            movie = next((t for t in self.titles["movie"] if str(t.uuid) == str(content_uuid)), None)
            if movie is None:
                return None
            rel = next((r for r in self.relations["movie"] if r.movie is movie
                        and r.stream_id == str(stream_id) and r.m3u_account.is_active), None)
            return {"kind": "movie", "uuid": str(movie.uuid), "name": movie.name or "",
                    "copy": (rel.m3u_account_id, rel.stream_id) if rel else None}
        for srel in self.relations["series"]:
            for erel in self.episodes.get(srel.id, []):
                if str(erel.episode.uuid) == str(content_uuid):
                    if stream_id is not None and erel.stream_id != str(stream_id):
                        continue
                    copy_ = ((srel.m3u_account_id, srel.external_series_id)
                             if stream_id is not None and srel.m3u_account.is_active else None)
                    return {"kind": "series", "uuid": str(srel.series.uuid),
                            "name": srel.series.name or "", "copy": copy_}
        return None

    def series_episodes(self, series_rel):
        return _dedupe_episodes(self.episodes.get(series_rel.id, []))


def make_fake_catalogue(plugin, spec):
    """Build a FakeCatalogue from a compact spec:

        {"accounts": {1: "Provider A", ...},
         "movies": [{"name": ..., "year": ..., "tmdb_id": ..., "poster": ...,
                     "copies": [(account_id, stream_id, category[, custom_properties]), ...]}, ...],
         "series": [{"name": ..., "year": ...,
                     "copies": [(account_id, external_series_id, category[, custom_properties]), ...],
                     "episodes": {(account_id, external_series_id):
                                  [(season, episode, title, stream_id[, media info]), ...]}}, ...]}

    Movie uuids end in ...-8000-<n>, series uuids in ...-9000-<n>.
    """
    accounts = {
        aid: SimpleNamespace(id=aid, name=name, is_active=True)
        for aid, name in spec["accounts"].items()
    }
    categories = {}

    def category(name):
        return categories.setdefault(name, SimpleNamespace(name=name)) if name else None

    def title(i, t, marker):
        logo = SimpleNamespace(url=t["poster"]) if t.get("poster") else None
        return SimpleNamespace(
            id=i, uuid=f"00000000-0000-4000-{marker}-{i:012d}", name=t["name"],
            year=t.get("year"), tmdb_id=t.get("tmdb_id", ""), imdb_id="",
            description=t.get("description", ""), rating="", genre=t.get("genre", ""),
            logo=logo,
        )

    titles = {"movie": [], "series": []}
    relations = {"movie": [], "series": []}
    episodes = {}
    rel_id = 0
    for i, m in enumerate(spec.get("movies", []), 1):
        movie = title(i, m, "8000")
        titles["movie"].append(movie)
        for account_id, stream_id, cat_name, *props in m["copies"]:
            rel_id += 1
            relations["movie"].append(SimpleNamespace(
                id=rel_id, movie=movie, m3u_account_id=account_id,
                m3u_account=accounts[account_id], stream_id=str(stream_id),
                category=category(cat_name), container_extension="mkv",
                custom_properties=props[0] if props else {},
            ))
    episode_objs = {}
    for i, s in enumerate(spec.get("series", []), 1):
        series = title(i, s, "9000")
        titles["series"].append(series)
        for account_id, external_id, cat_name, *props in s["copies"]:
            rel_id += 1
            rel = SimpleNamespace(
                id=rel_id, series=series, m3u_account_id=account_id,
                m3u_account=accounts[account_id], external_series_id=str(external_id),
                category=category(cat_name), custom_properties=props[0] if props else {},
            )
            relations["series"].append(rel)
            eps = []
            for season, number, name, stream_id, *media in s.get("episodes", {}).get((account_id, str(external_id)), []):
                # Copies share Episode objects, as in Dispatcharr.
                key = (i, season, number)
                if key not in episode_objs:
                    episode_objs[key] = SimpleNamespace(
                        uuid=f"00000000-0000-4000-a000-{i:04d}{season:04d}{number:04d}",
                        name=name, season_number=season, episode_number=number,
                        description="", air_date=None, rating="", duration_secs=None,
                        tmdb_id="", imdb_id="", custom_properties={},
                    )
                info = {"title": name}
                if media:
                    info["info"] = media[0]
                eps.append(SimpleNamespace(episode=episode_objs[key], stream_id=str(stream_id),
                                           custom_properties={"info": info}))
            episodes[rel.id] = eps
    return FakeCatalogue(plugin, titles, relations, episodes)
