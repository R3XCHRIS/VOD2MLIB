"""Persistent generation decisions; compare raw fields, without hashing media URLs.

Catalogue projections stay complete and category eligible. Only changed rows are
hydrated into Django models. External filesystem edits require inventory rebuild.
"""

import json
import os
from itertools import islice
from types import SimpleNamespace

try:
    from .inventory import BATCH_SIZE, LOOKUP_BATCH_SIZE
    from .metadata_filters import SETTING_KEYS, configuration
except ImportError:
    from inventory import BATCH_SIZE, LOOKUP_BATCH_SIZE
    from metadata_filters import SETTING_KEYS, configuration


def signature(settings, values):
    keys = (
        'root_folder', 'series_root_folder', 'dispatcharr_url', 'generate_nfo',
        'generate_series_nfo', 'nest_movies_by_category', 'nest_series_by_category',
        'dedupe_movies_across_categories', 'append_tmdb_id_to_folder',
        'tmdb_tag_format', 'omit_stream_id', 'nfo_omit_title',
        'media_library_enabled', 'media_tv_mode',
    )
    return json.dumps([[settings.get(k) for k in (*keys, *SETTING_KEYS)], values], default=str,
                      ensure_ascii=False, separators=(',', ':'))


def lookup(db, kind, keys):
    result = {}
    for offset in range(0, len(keys), LOOKUP_BATCH_SIZE):
        group = keys[offset:offset + LOOKUP_BATCH_SIZE]
        placeholders = ','.join('?' for _ in group)
        result.update((r['source'], (r['signature'], r['path'])) for r in db.execute(
            f'SELECT source,signature,path FROM generation_entries WHERE kind=? '
            f'AND source IN ({placeholders})', [kind, *group]))
    return result


def movie_key(row):
    # Dispatcharr can replace relation rows during sync. Provider/account identity
    # and category survive that replacement; the relation PK is only for hydration.
    return json.dumps([str(row[1]), str(row[2]), row[8]], separators=(',', ':'))


def movie_candidates(rec, query, settings):
    fields = ('id', 'm3u_account_id', 'stream_id', 'movie__uuid', 'movie__name',
              'movie__year', 'movie__tmdb_id', 'movie__imdb_id', 'category__name', 'movie__id', 'movie__rating')
    rules = configuration(settings)['movie']
    iterator = query.values_list(*fields).iterator(chunk_size=BATCH_SIZE)
    seen = set() if settings.get('dedupe_movies_across_categories', False) else None
    while True:
        with rec.measure('incremental_movie_read'):
            rows = list(islice(iterator, BATCH_SIZE))
        if not rows:
            break
        rec.report['timings']['incremental_movie_read']['items'] += len(rows)
        cached = lookup(rec.store.db, 'movie', [movie_key(r) for r in rows])
        changed = []
        for row in rows:
            rec.report['generation_checked'] += 1
            if not rules.evaluate(row[10], row[5], title=row[4])[0]:
                continue
            if seen is not None:
                if row[3] in seen:
                    rec.report['generation_deduped'] += 1
                    continue
                seen.add(row[3])
            obj = SimpleNamespace(id=row[9], uuid=row[3], name=row[4], year=row[5],
                                  tmdb_id=row[6], imdb_id=row[7])
            owned = rec.owns(obj, 'movie')
            value = signature(settings, [*row[1:], owned])
            key = movie_key(row)
            if cached.get(key, (None,))[0] == value:
                rec.report['generation_unchanged'] += 1
                continue
            if owned:
                rec.cache_complete('movie', key, value, '')
                continue
            changed.append((row[0], key, value, obj, row))
        if rec.bootstrap_movies:
            # Existing inventory already verified these outputs. Seed the first
            # generation decisions from it without re-reading every STRM on disk.
            candidates = {}
            for pk, key, value, obj, row in changed:
                if key in cached:
                    continue
                folder, name, *_ = rec.plugin._movie_target_paths(
                    obj, settings.get('root_folder', '/VODS/Movies'), row[8] or '',
                    settings.get('nest_movies_by_category', False),
                    settings.get('append_tmdb_id_to_folder', False),
                    settings.get('tmdb_tag_format') or 'plex',
                )
                path = os.path.abspath(os.path.join(folder, name))
                url = rec.plugin._build_proxy_url(
                    (settings.get('dispatcharr_url') or '').rstrip('/'), 'movie',
                    row[3], row[2], settings.get('omit_stream_id', False),
                )
                candidates[key] = (path, url)
            known = {}
            paths = [value[0] for value in candidates.values()]
            for offset in range(0, len(paths), LOOKUP_BATCH_SIZE):
                group = paths[offset:offset + LOOKUP_BATCH_SIZE]
                placeholders = ','.join('?' for _ in group)
                known.update(rec.store.db.execute(
                    f'SELECT path,strm_url FROM files WHERE path IN ({placeholders})', group))
            pending = []
            for item in changed:
                pk, key, value, obj, row = item
                expected = candidates.get(key)
                if expected and known.get(expected[0], '').strip() == expected[1].strip():
                    rec.cache_complete('movie', key, value, expected[0])
                    rec.report['generation_unchanged'] += 1
                else:
                    pending.append(item)
            changed = pending
        # Bound SQL parameter counts and preserve the original projection order,
        # including deterministic first-category wins across projection batches.
        for offset in range(0, len(changed), LOOKUP_BATCH_SIZE):
            group = changed[offset:offset + LOOKUP_BATCH_SIZE]
            models = {r.id: r for r in query.filter(id__in=[r[0] for r in group])}
            for pk, key, value, obj, row in group:
                rel = models.get(pk)
                if rel is None:
                    continue  # Concurrent source removal is not a completion.
                rel._generation_signature = (key, value)
                rec.report['generation_candidates'] += 1
                yield rel
    with rec.store.db:
        rec.store.db.execute("INSERT OR REPLACE INTO generation_state VALUES ('movies_initialized','1')")
