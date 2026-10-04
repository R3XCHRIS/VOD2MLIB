import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from plugin import Plugin
from tests.test_reconciliation import library, media, relation, Query


def test_next_movie_run_removes_filtered_strm_and_generated_nfo_even_with_batch_limit(library):
    library.rows['movies'].extend([relation(media(1, year=1914)), relation(media(2, year=2020))])
    assert library.run()['created_strm'] == 2
    result = library.run(movie_earliest_year='2000', batch_size='1')
    assert result['reconciliation']['filter_movie_deleted'] == 1
    assert result['reconciliation']['deleted_nfo'] == 1
    assert not list(Path(library.settings['root_folder']).rglob('*1914*'))
    assert len(list(Path(library.settings['root_folder']).rglob('*.strm'))) == 1


def test_filter_preview_is_read_only_and_agrees_with_next_generation(library):
    movie = media(1, year=1914)
    library.rows['movies'].append(relation(movie))
    library.run()
    root = Path(library.settings['root_folder'])
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
    result = library.run('preview_cleanup', movie_earliest_year='2000', media_library_enabled=True)
    assert result['reconciliation']['filter_candidates'] == 1
    assert result['reconciliation']['candidate'] == 1
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}
    assert library.run(movie_earliest_year='2000')['reconciliation']['filter_deleted'] == 1


def test_series_filters_remove_all_episode_output_without_fetching_rejected_show(library):
    show = media(10, year=2019, genre='Horror')
    library.rows['series'].append(relation(show, 'series'))
    for n in range(1, 4):
        library.rows['episodes'].append(relation(media(n, series=show, season_number=1, episode_number=n), 'episode'))
    library.run('generate_series', refresh_existing=True)
    before = len(library.calls)
    result = library.run('generate_series', series_genre_exclude='Horror', refresh_existing=True)
    assert result['reconciliation']['filter_series_deleted'] == 3
    assert result['reconciliation']['deleted_nfo'] == 4
    assert len(library.calls) == before
    assert not list(Path(library.settings['series_root_folder']).rglob('*.strm'))
    assert not list(Path(library.settings['series_root_folder']).rglob('*.nfo'))


def test_edited_strm_is_preserved_and_edited_nfo_is_archived(library):
    library.rows['movies'].extend([relation(media(1, year=1914)), relation(media(2, year=1914))])
    library.run()
    paths = sorted(Path(library.settings['root_folder']).rglob('*.strm'))
    paths[0].write_text('custom stream')
    nfo = paths[1].with_suffix('.nfo')
    nfo.write_text('custom metadata')
    result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_preserved'] == 1
    assert result['reconciliation']['filter_deleted'] == 1
    assert paths[0].read_text() == 'custom stream'
    assert not nfo.exists()
    archived = next((library.tmp / 'state' / 'filtered-nfo').rglob(nfo.name))
    assert archived.read_text() == 'custom metadata'
    assert result['reconciliation']['filter_nfo_folders_archived'] == 1


def test_passing_source_protects_shared_output_then_all_rejections_remove_it(library):
    library.rows['movies'].extend([relation(media(1, rating='2'), stream='low'),
                                  relation(media(1, rating='9'), stream='high')])
    library.run()
    path = next(Path(library.settings['root_folder']).rglob('*.strm'))
    assert library.run(movie_minimum_score='8')['reconciliation']['filter_deleted'] == 0
    assert path.exists()
    assert library.run(movie_minimum_score='10')['reconciliation']['filter_deleted'] == 1
    assert not path.exists()


def test_unknown_metadata_policy_and_missing_source_are_distinct(library):
    library.rows['movies'].append(relation(media(1, year=None)))
    library.run()
    assert library.run(movie_earliest_year='2000')['reconciliation']['filter_deleted'] == 0
    assert library.run(movie_earliest_year='2000', movie_missing_metadata='reject')['reconciliation']['filter_deleted'] == 1
    library.rows['movies'][:] = [relation(media(2, year=1914))]
    library.run()
    path = next(Path(library.settings['root_folder']).rglob('*.strm'))
    library.rows['movies'].clear()
    assert library.run(movie_earliest_year='2000', movie_missing_metadata='reject')['reconciliation']['filter_deleted'] == 0
    assert path.exists()


def test_metadata_lookup_failure_stops_before_any_filter_deletion(library, monkeypatch):
    library.rows['movies'].append(relation(media(1, year=1914)))
    library.run()
    path = next(Path(library.settings['root_folder']).rglob('*.strm'))
    original = Query.filter
    def fail(self, **kwargs):
        if 'stream_id__in' in kwargs:
            raise OSError('lookup unavailable')
        return original(self, **kwargs)
    monkeypatch.setattr(Query, 'filter', fail)
    result = library.run(movie_earliest_year='2000')
    assert result['status'] == 'error' and result['reconciliation']['filter_deleted'] == 0
    assert path.exists()


def test_scheduled_runs_use_current_settings_and_ignore_legacy_snapshot(monkeypatch):
    saved = {'movie_earliest_year': '2000', 'series_genre_exclude': 'Horror',
             'root_folder': '/saved/Movies', 'batch_size': '250'}
    monkeypatch.setitem(sys.modules, 'apps.plugins.models', NS(
        PluginConfig=NS(objects=NS(get=lambda **kwargs: NS(settings=saved)))))
    snapshot = {'movie_earliest_year': '1900', 'series_title_exclude': '^AR',
                'root_folder': '/scheduled/Movies', 'batch_size': '100'}
    before = dict(snapshot)
    current = Plugin._scheduled_settings(snapshot)
    assert current['movie_earliest_year'] == '2000'
    assert current['series_genre_exclude'] == 'Horror'
    assert current['series_title_exclude'] == ''
    assert current['root_folder'] == '/saved/Movies' and current['batch_size'] == '250'
    assert snapshot == before


def test_filter_lookups_and_deletions_finish_across_bounded_batches(library, monkeypatch):
    import filter_cleanup
    monkeypatch.setattr(filter_cleanup, 'LOOKUP_BATCH_SIZE', 2)
    library.rows['movies'].extend(relation(media(n, year=1914)) for n in range(1, 8))
    library.run()
    result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_deleted'] == 7
    assert not list(Path(library.settings['root_folder']).rglob('*.strm'))


def test_later_metadata_batch_failure_does_not_delete_earlier_rejections(library, monkeypatch):
    import filter_cleanup
    monkeypatch.setattr(filter_cleanup, 'LOOKUP_BATCH_SIZE', 1)
    library.rows['movies'].extend(relation(media(n, year=1914)) for n in (1, 2))
    library.run()
    original = Query.filter
    calls = []
    def fail(self, **kwargs):
        if 'stream_id__in' in kwargs:
            calls.append(kwargs)
            if len(calls) == 2:
                raise OSError('later batch failed')
        return original(self, **kwargs)
    monkeypatch.setattr(Query, 'filter', fail)
    result = library.run(movie_earliest_year='2000')
    assert result['status'] == 'error' and result['reconciliation']['filter_deleted'] == 0
    assert len(list(Path(library.settings['root_folder']).rglob('*.strm'))) == 2


def test_movies_action_does_not_remove_series_output(library):
    show = media(10, year=1914)
    library.rows['series'].append(relation(show, 'series'))
    library.rows['episodes'].append(relation(media(11, series=show, season_number=1, episode_number=1), 'episode'))
    library.run('generate_series')
    path = next(Path(library.settings['series_root_folder']).rglob('*.strm'))
    assert library.run(series_earliest_year='2000')['reconciliation']['filter_series_deleted'] == 0
    assert path.exists()
    assert library.run('generate_series', series_earliest_year='2000')['reconciliation']['filter_series_deleted'] == 1


def test_discovery_failure_does_not_silently_skip_filter_cleanup(library, monkeypatch):
    from reconciliation import Reconciliation
    library.rows['movies'].append(relation(media(1, year=1914)))
    library.run()
    def fail(*args, **kwargs):
        raise OSError('discovery unavailable')
    monkeypatch.setattr(Reconciliation, 'adopt', fail)
    result = library.run(movie_earliest_year='2000')
    assert result['status'] == 'error'
    assert result['reconciliation']['filter_deleted'] == 0
    assert len(list(Path(library.settings['root_folder']).rglob('*.strm'))) == 1


def test_nfo_failure_reports_removed_strm_and_retries_remaining_nfo(library, monkeypatch):
    import inventory
    library.rows['movies'].append(relation(media(1, year=1914)))
    library.run()
    path = next(Path(library.settings['root_folder']).rglob('*.strm'))
    original = inventory.os.remove
    def fail(nfo):
        if str(nfo).endswith('.nfo'):
            raise OSError('nfo unavailable')
        return original(nfo)
    with monkeypatch.context() as patch:
        patch.setattr(inventory.os, 'remove', fail)
        result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_deleted'] == 1
    assert result['reconciliation']['filter_errors'] == 1
    assert not path.exists() and path.with_suffix('.nfo').exists()
    result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_missing'] == 1
    assert not path.with_suffix('.nfo').exists()


def test_filter_removals_commit_once_per_bounded_batch(library, monkeypatch):
    import filter_cleanup
    from inventory import InventoryStore
    monkeypatch.setattr(filter_cleanup, 'LOOKUP_BATCH_SIZE', 2)
    library.rows['movies'].extend(relation(media(n, year=1914)) for n in range(1, 8))
    library.run()
    commits = []
    original = InventoryStore.__init__
    def instrument(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.db.set_trace_callback(lambda sql: commits.append(sql) if
            sql == 'COMMIT' and getattr(self, '_forget_batch_active', False) else None)
    monkeypatch.setattr(InventoryStore, '__init__', instrument)
    result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_deleted'] == 7
    assert len(commits) == 4
    timing = result['reconciliation']['timings']['filter_cleanup_batch']
    assert timing['calls'] == 4 and timing['items'] == 7


def test_interrupted_removal_batch_retains_inventory_for_safe_retry(library):
    from inventory import InventoryStore
    library.rows['movies'].extend(relation(media(n, year=1914)) for n in (1, 2))
    library.run()
    store = InventoryStore(library.tmp / 'state')
    rows = list(store.rows())
    with pytest.raises(RuntimeError, match='interrupted'):
        with store.forget_batch():
            assert store.delete(rows[0], [library.settings['root_folder']], include_nfo=True) == 'deleted'
            raise RuntimeError('interrupted')
    assert not Path(rows[0]['path']).exists()
    assert len(list(store.rows())) == 2
    store.close()
    result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_missing'] == 1
    assert result['reconciliation']['filter_deleted'] == 1
    store = InventoryStore(library.tmp / 'state')
    assert not list(store.rows())
    store.close()


def test_removal_batch_prunes_shared_directory_once(tmp_path, monkeypatch):
    from inventory import InventoryStore
    root = tmp_path / 'output'
    season = root / 'Show' / 'Season 1'
    season.mkdir(parents=True)
    store = InventoryStore(tmp_path / 'state')
    original = Path.rmdir
    attempts = []
    def track(self):
        attempts.append(str(self))
        return original(self)
    monkeypatch.setattr(Path, 'rmdir', track)
    from media_library import Identity
    records = []
    for n in range(5):
        path = season / f'{n}.strm'
        path.write_text(f'http://example.invalid/{n}')
        records.append((str(path), Identity('series', 'Show', '2010', None, None),
                        f'["episode","1","{n}"]', (1, n), None))
    store.record_many(records)
    stats = {}
    with store.forget_batch():
        for row in list(store.rows()):
            assert store.delete(row, [str(root)], stats=stats) == 'deleted'
        assert season.exists()
    assert attempts.count(str(season)) == 1
    assert stats['removed_dirs'] == 2
    assert root.is_dir() and not list(root.iterdir())
    store.close()


def test_keyset_inventory_batches_do_not_skip_rows_during_deletion(library):
    from inventory import InventoryStore
    library.rows['movies'].extend(relation(media(n)) for n in range(1, 8))
    library.run()
    store = InventoryStore(library.tmp / 'state')
    removed = []
    for batch in store.row_batches(batch=2):
        with store.forget_batch():
            for row in batch:
                removed.append(row['path'])
                assert store.delete(row, [library.settings['root_folder']]) == 'deleted'
    assert len(set(removed)) == 7 and not list(store.rows())
    store.close()


def test_parallel_cleanup_keeps_shared_nfo_when_edited_episode_remains(library):
    show = media(10, year=2019)
    library.rows['series'].append(relation(show, 'series'))
    for n in range(1, 4):
        library.rows['episodes'].append(relation(media(n, series=show, season_number=1, episode_number=n), 'episode'))
    library.run('generate_series')
    root = Path(library.settings['series_root_folder'])
    paths = sorted(root.rglob('*.strm'))
    paths[-1].write_text('custom stream')
    result = library.run('generate_series', series_earliest_year='2020')
    assert result['reconciliation']['filter_deleted'] == 2
    assert result['reconciliation']['filter_preserved'] == 1
    assert paths[-1].read_text() == 'custom stream'
    assert len(list(root.rglob('tvshow.nfo'))) == 1
    assert result['reconciliation']['timings']['cleanup_strm_io']['calls'] == 3


def test_worker_partial_unlink_failure_is_counted_and_retryable(library, monkeypatch):
    import inventory
    library.rows['movies'].append(relation(media(1, year=1914)))
    library.run()
    path = next(Path(library.settings['root_folder']).rglob('*.strm'))
    original = inventory.os.remove
    def fail_after_unlink(target):
        original(target)
        if str(target).endswith('.strm'):
            raise OSError('unlink interrupted')
    with monkeypatch.context() as patch:
        patch.setattr(inventory.os, 'remove', fail_after_unlink)
        result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_deleted'] == 1
    assert result['reconciliation']['filter_errors'] == 1
    assert not path.exists() and path.with_suffix('.nfo').exists()
    result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_missing'] == 1
    assert not path.with_suffix('.nfo').exists()
