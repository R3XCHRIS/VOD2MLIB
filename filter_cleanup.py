"""Remove verified managed output rejected by current Dispatcharr metadata."""

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

try:
    from .inventory import LOOKUP_BATCH_SIZE, remove_strm
    from .metadata_filters import configuration, evaluate_metadata
    from .orphan_nfo import plan as nfo_plan, archive as archive_nfos
except ImportError:
    from inventory import LOOKUP_BATCH_SIZE, remove_strm
    from metadata_filters import configuration, evaluate_metadata
    from orphan_nfo import plan as nfo_plan, archive as archive_nfos


class FilterCleanupError(RuntimeError):
    pass


ACTION_KINDS = {'generate_movies': ('movie',), 'generate_series': ('series',),
                'rescan_all': ('movie', 'series'), 'preview_cleanup': ('movie', 'series'),
                'selective_cleanup': ('movie', 'series')}


def active(rules):
    return any((rules.minimum_score is not None, rules.earliest_year is not None,
                rules.latest_year is not None, rules.include, rules.exclude,
                rules.title_include is not None, rules.title_exclude is not None))


def applies(settings, action):
    rules = configuration(settings)
    return any(active(rules[kind]) for kind in ACTION_KINDS.get(action, ()))


def cleanup(rec, action):
    kinds = ACTION_KINDS.get(action, ())
    rules = configuration(rec.settings)
    kinds = tuple(kind for kind in kinds if active(rules[kind]))
    if not kinds:
        return
    from apps.vod.models import M3UMovieRelation, M3UEpisodeRelation

    rec.progress('Checking managed files against current metadata filters')
    db = rec.store.db
    db.execute('CREATE TEMP TABLE IF NOT EXISTS filter_sources(source TEXT PRIMARY KEY, passed INTEGER NOT NULL)')
    db.execute('DELETE FROM filter_sources')
    db.execute('CREATE TEMP TABLE IF NOT EXISTS filter_handled(path TEXT PRIMARY KEY)')
    db.execute('DELETE FROM filter_handled')
    rec.filter_cleanup_active = True
    placeholders = ','.join('?' for _ in kinds)
    cursor = db.execute(
        f'SELECT DISTINCT s.source FROM sources s JOIN files f ON f.path=s.path '
        f'WHERE f.kind IN ({placeholders}) ORDER BY s.source', kinds)
    # Complete all bounded native lookups before allowing any filter deletion.
    # Missing/ambiguous source mappings stay unknown and protect shared output.
    try:
        orphan_candidates = nfo_plan(rec, kinds, rules)
        with rec.measure('filter_metadata_read'):
            while True:
                sources = cursor.fetchmany(LOOKUP_BATCH_SIZE)
                if not sources:
                    break
                groups = defaultdict(dict)
                for (source,) in sources:
                    try:
                        value = json.loads(source)
                        if (not isinstance(value, list) or len(value) != 3
                                or value[0] not in ('movie', 'episode')
                                or not isinstance(value[1], str) or not value[1].isdigit()
                                or not isinstance(value[2], str) or not value[2]):
                            continue
                        groups[(value[0], int(value[1]))].setdefault(value[2], []).append(source)
                    except (ValueError, TypeError):
                        continue
                for (source_kind, account), requested in groups.items():
                    kind = 'movie' if source_kind == 'movie' else 'series'
                    if kind not in kinds:
                        continue
                    model = M3UMovieRelation if kind == 'movie' else M3UEpisodeRelation
                    prefix = 'movie' if kind == 'movie' else 'episode__series'
                    fields = ['stream_id', f'{prefix}__rating', f'{prefix}__year']
                    if kind == 'series':
                        fields.append(f'{prefix}__genre')
                    fields.append(f'{prefix}__name')
                    decisions = defaultdict(list)
                    query = model.objects.filter(m3u_account_id=account, stream_id__in=list(requested))
                    for row in query.values_list(*fields).iterator(chunk_size=LOOKUP_BATCH_SIZE):
                        stream = str(row[0])
                        if stream not in requested:
                            raise ValueError('Unexpected filter metadata result')
                        decisions[stream].append(evaluate_metadata(rules[kind], kind, row[1:])[0])
                    with db:
                        db.executemany('INSERT OR REPLACE INTO filter_sources VALUES (?,?)',
                                       [(source, int(any(passed)))
                                        for stream, passed in decisions.items()
                                        for source in requested[stream]])
    except Exception as error:
        raise FilterCleanupError(
            f'Filter metadata lookup failed; no filter removals applied ({type(error).__name__})'
        ) from error
    sql = (
        f'SELECT f.* FROM files f WHERE f.kind IN ({placeholders}) '
        'AND EXISTS (SELECT 1 FROM sources s WHERE s.path=f.path) '
        'AND NOT EXISTS (SELECT 1 FROM sources s LEFT JOIN filter_sources d ON d.source=s.source '
        'WHERE s.path=f.path AND (d.passed IS NULL OR d.passed=1)) '
        'AND f.path>? ORDER BY f.path LIMIT ?')
    dry_run = action == 'preview_cleanup'
    rec.filter_preview_removals = {}
    def remove_one(row):
        with rec.measure('cleanup_strm_io', 1, worker=True):
            was_existing = os.path.lexists(row['path'])
            try:
                return remove_strm(row, rec.roots, dry_run), None, was_existing
            except OSError as error:
                return None, error, was_existing

    with rec.measure('filter_cleanup'), ThreadPoolExecutor(max_workers=3) as workers:
        last_path = ''
        while True:
            reader = db.execute(sql, (*kinds, last_path, LOOKUP_BATCH_SIZE))
            batch = reader.fetchall()
            reader.close()
            if not batch:
                break
            last_path = batch[-1]['path']
            with rec.measure('filter_cleanup_batch', len(batch)), rec.store.forget_batch():
                for row, (outcome, error, was_existing) in zip(batch, workers.map(remove_one, batch)):
                    rec.report['filter_checked'] += 1
                    db.execute('INSERT OR IGNORE INTO filter_handled VALUES (?)', (row['path'],))
                    try:
                        if error is not None:
                            raise error
                        outcome = rec.store.finish_delete(row, rec.roots, outcome, include_nfo=True,
                                                          dry_run=dry_run, stats=rec.report)
                        if dry_run and outcome in ('candidate', 'missing'):
                            rec.filter_preview_removals[row['path']] = json.loads(row['nfos'])
                        rec.report[outcome] += 1
                        counter = {'candidate': 'filter_candidates', 'deleted': 'filter_deleted',
                                   'preserved': 'filter_preserved', 'missing': 'filter_missing'}[outcome]
                        rec.report[counter] += 1
                        if outcome == 'deleted':
                            rec.report[f'filter_{row["kind"]}_deleted'] += 1
                        rec.logger.info('%s: %s (metadata filters)', outcome, row['path'])
                    except OSError as error:
                        if was_existing and not os.path.lexists(row['path']):
                            rec.report['deleted'] += 1
                            rec.report['filter_deleted'] += 1
                            rec.report[f'filter_{row["kind"]}_deleted'] += 1
                        rec.report['errors'] += 1
                        rec.report['filter_errors'] += 1
                        rec.logger.error('Filter cleanup failed for %s: %s', row['path'], error)
            rec.progress(
                f"Applying metadata filters: {rec.report['filter_checked']:,} checked, "
                f"{rec.report['filter_deleted']:,} removed, "
                f"{rec.report['filter_preserved']:,} protected; "
                f"{rec.report['filter_errors']:,} errors")
    archive_nfos(rec, orphan_candidates, dry_run)
