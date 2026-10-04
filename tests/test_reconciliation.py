"""Reconciliation lifecycle, adapter, ownership and persistence regression tests."""

import io
import json
import logging
import os
import sqlite3
import sys
from itertools import count
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from inventory import BATCH_SIZE, InventoryStore, action_lock, contained
from media_library import EmbyAdapter, Identity, OwnedMedia, Snapshot
from plugin import Plugin
from reconciliation import Reconciliation

LOG = logging.getLogger(__name__)


def test_containment_checks_matching_root_first_without_skipping_resolution(tmp_path, monkeypatch):
    movie, series = tmp_path / 'Movies', tmp_path / 'Series'
    series.mkdir()
    calls = []
    original = os.path.realpath
    def resolve(path):
        calls.append(str(path))
        return original(path)
    monkeypatch.setattr(os.path, 'realpath', resolve)
    path = series / 'Show' / 'episode.strm'
    assert contained(path, [movie, series])
    assert calls == [str(path), str(series)]
    # Reordering never makes lexical containment sufficient for an escaped path.
    assert not contained(series / '..' / 'outside.strm', [movie, series])


def test_series_ownership_matches_once_and_preserves_episode_exclusions(library, monkeypatch):
    show = media(10)
    rec = Reconciliation(library.p, {**library.settings, 'media_tv_mode': 'episodes'}, LOG, library.tmp / 'ownership')
    rec.snapshot = Snapshot([owned(show, 'series', [(1, 1), (1, 2)])])
    calls = []
    original = rec.snapshot.matches
    monkeypatch.setattr(rec.snapshot, 'matches', lambda identity: calls.append(identity) or original(identity))
    try:
        check = rec.series_ownership(show)
        assert check((1, 1)) and check((1, 2)) and not check((1, 3))
        assert len(calls) == 1 and rec.report['excluded'] == 2
    finally:
        rec.store.close()


@pytest.mark.parametrize('value', ['0', '7', '3.0', '', '-1', True, None, '٣'])
def test_invalid_worker_count_stops_before_reconciliation(library, monkeypatch, value):
    monkeypatch.setattr(Reconciliation, 'prepare', lambda *args: pytest.fail('Reconciliation started'))
    result = library.run('generate_series', series_workers=value)
    assert result['status'] == 'error' and 'workers' in result['message']


def test_series_uses_configured_worker_pool_without_changing_output(library, monkeypatch):
    import plugin
    show = media(10)
    library.rows['series'].append(relation(show, 'series'))
    library.rows['episodes'].append(relation(
        media(1, series=show, season_number=1, episode_number=1), 'episode'))
    pools = []
    executor = plugin.ThreadPoolExecutor
    def pool(*args, **kwargs):
        pools.append(kwargs['max_workers'])
        return executor(*args, **kwargs)
    monkeypatch.setattr(plugin, 'ThreadPoolExecutor', pool)
    result = library.run('generate_series', series_workers='6', refresh_existing=True)
    assert pools == [6] and result['episodes_created'] == 1 and result['errors'] == 0
    metrics = result['reconciliation']['timings']
    assert metrics['output_guard']['calls'] == 1
    assert metrics['episode_ownership']['calls'] == 1
    assert metrics['inventory_enqueue']['items'] == 1
    assert metrics['checkpoint_enqueue']['items'] == 1


def test_drain_frees_checkpoints_before_inventory_and_never_commits_after_failure(library, monkeypatch):
    rec = Reconciliation(library.p, library.settings, LOG, library.tmp / 'drain-slots')
    rec.queue.put(('pending record',))
    rec.cache_queue.put(('episode', 'source', 'signature', '/pending'))
    def fail(records):
        assert rec.cache_queue.empty() and rec.queue.empty()
        assert rec.store.db.execute('SELECT COUNT(*) FROM generation_entries').fetchone()[0] == 0
        raise OSError('Inventory write failed')
    monkeypatch.setattr(rec.store, 'record_many', fail)
    try:
        with pytest.raises(OSError, match='Inventory write failed'):
            rec.drain()
        assert rec.cancelled.is_set()
        assert rec.store.db.execute('SELECT COUNT(*) FROM generation_entries').fetchone()[0] == 0
    finally:
        rec.store.close()


def test_frozen_series_catalogue_produces_identical_output_across_worker_counts(library):
    # Provider/catalogue inputs stay fixed: changing concurrency must not add files.
    for show_id, count_ in [(101, 10), (102, 4), (103, 25)]:
        show = media(show_id)
        library.rows['series'].append(relation(show, 'series'))
        for n in range(1, count_ + 1):
            ep = media(show_id * 100 + n, series=show, season_number=1, episode_number=n)
            rel = relation(ep, 'episode')
            library.rows['episodes'].append(rel)
            if n == 1:
                library.rows['episodes'].append(relation(ep, 'episode', stream='duplicate'))
    manifests = []
    for workers in ('3', '6'):
        root = library.tmp / ('series-workers-' + workers)
        result = library.run('generate_series', series_root_folder=str(root),
                             series_workers=workers, refresh_existing=True)
        assert result['errors'] == 0 and result['episodes_created'] == 39
        assert result['nfo_created'] == 42
        manifests.append({str(p.relative_to(root)): p.read_bytes()
                          for p in root.rglob('*') if p.is_file()})
    assert manifests[0] == manifests[1]


def test_series_generation_reports_work_and_avoids_repeated_directory_setup(library, monkeypatch):
    show = media(10)
    library.rows['series'].append(relation(show, 'series'))
    for n in range(1, 5):
        library.rows['episodes'].append(relation(
            media(n, series=show, season_number=1, episode_number=n), 'episode'))
    directories, live = [], []
    original = os.makedirs
    def mkdir(path, *args, **kwargs):
        if Path(path).name == 'Season 01':
            directories.append(str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'makedirs', mkdir)
    monkeypatch.setattr(library.p, '_progress', live.append, raising=False)
    result = library.run('generate_series', refresh_existing=True)
    assert result['errors'] == 0 and result['episodes_created'] == 4
    assert len(directories) == 1
    report = result['reconciliation']
    assert report['series_completed'] == 1 and report['episodes_evaluated'] == 4
    timings = report['timings']
    assert timings['episode_query']['calls'] == 1
    assert timings['series_files']['items'] == 4
    assert timings['strm_write']['items'] == 4
    assert timings['episode_nfo']['items'] == 4
    assert timings['inventory_record']['items'] == 4
    assert timings['inventory_checkpoint']['items'] == 4
    assert timings['series_files']['cpu_clock'] == 'thread'
    assert '1 completed' in live[-1] and '4 episodes evaluated' in live[-1]
    # Cached database episodes create no output or directories and never fetch providers.
    directories.clear()
    result = library.run('generate_series', refresh_existing=True)
    assert result['episodes_created'] == 0 and not directories
    assert result['reconciliation']['generation_unchanged'] == 4
    assert 'strm_write' not in result['reconciliation']['timings']


def test_worker_timing_uses_thread_cpu_and_live_snapshots_are_independent(library, monkeypatch):
    rec = Reconciliation(library.p, library.settings, LOG, library.tmp / 'timing-worker')
    try:
        wall, cpu = iter([10.0, 12.0]), iter([2.0, 2.5])
        monkeypatch.setattr('reconciliation.time.perf_counter', lambda: next(wall))
        monkeypatch.setattr('reconciliation.time.thread_time', lambda: next(cpu))
        monkeypatch.setattr('reconciliation.time.process_time', lambda: pytest.fail('Process CPU double-counts workers'))
        with rec.measure('worker', 1, worker=True):
            pass
        live = rec.telemetry()
        assert live['timings']['worker']['cpu_seconds'] == 0.5
        assert live['timings']['worker']['cpu_clock'] == 'thread'
        live['timings']['worker']['calls'] = 999
        assert rec.report['timings']['worker']['calls'] == 1
    finally:
        rec.store.close()


def test_episode_sql_wrapper_records_counts_without_query_data(library, monkeypatch):
    from contextlib import contextmanager
    rec = Reconciliation(library.p, library.settings, LOG, library.tmp / 'sql-timing')
    wrappers = []
    @contextmanager
    def execute_wrapper(wrapper):
        wrappers.append(wrapper)
        try:
            yield
        finally:
            wrappers.pop()
    monkeypatch.setitem(sys.modules, 'django.conf', NS(settings=NS(configured=True)))
    monkeypatch.setitem(sys.modules, 'django.db', NS(connection=NS(execute_wrapper=execute_wrapper)))
    try:
        with rec.episode_query():
            assert wrappers[0](lambda *args: 'result', 'private SQL', ['secret'], False, {}) == 'result'
        assert not wrappers
        assert rec.report['timings']['episode_sql']['calls'] == 1
        assert 'secret' not in json.dumps(rec.telemetry())
        with pytest.raises(ValueError):
            with rec.episode_query():
                raise ValueError('failed')
        assert not wrappers and rec.report['timings']['episode_query']['calls'] == 2
    finally:
        rec.store.close()


class Query:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def all(self):
        return self

    def select_related(self, *args):
        return self

    def only(self, *args):
        return self

    def values_list(self, *fields):
        def resolve(row, field):
            for part in field.split("__"):
                if part.endswith("_id") and not hasattr(row, part):
                    row = getattr(row, part[:-3]).id
                else:
                    row = getattr(row, part) if row is not None else None
            return row
        return Query([tuple(resolve(row, field) for field in fields) for row in self.rows])

    def order_by(self, *args):
        return self

    def count(self):
        return len(self.rows)

    def iterator(self, **kwargs):
        yield from list(self.rows)

    def filter(self, **kwargs):
        def matches(rel):
            for key, expected in kwargs.items():
                is_in = key.endswith('__in')
                field = key[:-4] if is_in else key
                value = rel
                for part in field.split('__'):
                    if part.endswith('_id') and not hasattr(value, part):
                        value = getattr(value, part[:-3]).id
                    else:
                        value = getattr(value, part)
                if not (value in expected if is_in else value == expected):
                    return False
            return True

        return Query([r for r in self.rows if matches(r)])


def media(id, kind="movie", **values):
    defaults = dict(
        id=id,
        uuid=f"uuid-{id}",
        name=f"Title {id}",
        year=2000,
        tmdb_id=str(id),
        imdb_id="",
        description="",
        genre="",
        rating="",
        logo=None,
    )
    defaults.update(values)
    return NS(**defaults)


RELATION_IDS = count(1)


def relation(obj, kind="movie", stream=None):
    return NS(
        id=next(RELATION_IDS),
        **{kind: obj},
        m3u_account_id=1,
        m3u_account=NS(id=1),
        stream_id=stream or str(obj.id),
        category=NS(name="Action"),
        custom_properties={"episodes_fetched": True},
        external_series_id=str(obj.id),
    )


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv("VOD2MLIB_STATE_DIR", str(tmp_path / "state"))
    rows = {"movies": [], "series": [], "episodes": []}
    models = NS(
        M3UMovieRelation=NS(objects=Query(rows["movies"])),
        M3USeriesRelation=NS(objects=Query(rows["series"])),
        M3UEpisodeRelation=NS(objects=Query(rows["episodes"])),
    )
    monkeypatch.setitem(sys.modules, "apps.vod.models", models)
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "apps.vod.tasks",
        NS(refresh_series_episodes=lambda **kw: calls.append(kw)),
    )
    p = Plugin()
    monkeypatch.setattr(p, "_eligible_vod_relations", lambda query, _: query)
    monkeypatch.setattr(
        p, "_scan_all_vods", lambda *_: {"status": "ok", "message": "Scan complete"}
    )
    settings = dict(
        root_folder=str(tmp_path / "Movies"),
        series_root_folder=str(tmp_path / "Series"),
        dispatcharr_url="http://dispatcharr.test:9191",
        media_library_ids="library",
        batch_size="all",
        series_batch_size="all",
        generate_nfo=True,
        generate_series_nfo=True,
    )
    state = {"snapshot": Snapshot([]), "error": None, "requests": []}

    class Adapter:
        def get_snapshot(self, libraries, media_types):
            state["requests"].append((libraries, media_types))
            if state["error"]:
                raise state["error"]
            return state["snapshot"]

        def list_libraries(self):
            return [
                {"Id": "library", "Name": "Movies"},
                {"Id": "a", "Name": "TV Shows"},
                {"Id": "b", "Name": "Recorded TV"},
            ]

    monkeypatch.setattr("reconciliation.create_adapter", lambda _: Adapter())
    monkeypatch.setattr("plugin.create_adapter", lambda _: Adapter())

    def run(action="generate_movies", **updates):
        return p._run_locked_action(
            action, {}, {"logger": LOG, "settings": {**settings, **updates}}
        )

    return NS(
        p=p,
        rows=rows,
        state=state,
        settings=settings,
        run=run,
        tmp=tmp_path,
        calls=calls,
    )


def owned(obj, kind="movie", episodes=()):
    return OwnedMedia(
        Identity(kind, obj.name, obj.year, obj.tmdb_id, obj.imdb_id), set(episodes)
    )


def test_generation_acquisition_next_batch_removal_and_restoration(library):
    a, b, c = [media(i) for i in (1, 2, 3)]
    library.rows["movies"].extend([relation(a), relation(b)])
    first = library.run()
    assert first["created_strm"] == 2
    paths = list(Path(library.settings["root_folder"]).rglob("*.strm"))
    assert len(paths) == 2
    library.state["snapshot"] = Snapshot([owned(b)])
    library.rows["movies"].insert(0, relation(c))
    result = library.run(media_library_enabled=True, batch_size="1")
    assert result["created_strm"] == 1
    assert result["reconciliation"]["deleted"] == 1
    assert not next(p for p in paths if "Title 2" in str(p)).exists()
    library.state["snapshot"] = Snapshot([])
    result = library.run(media_library_enabled=True)
    assert result["created_strm"] == 1
    assert len(list(Path(library.settings["root_folder"]).rglob("*.strm"))) == 3


@pytest.mark.parametrize("mode,remaining", [("show", 0), ("episodes", 1)])
def test_tv_modes_and_existing_folder_shortcut(library, mode, remaining):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    for n in (1, 2):
        ep = media(n, series=show, season_number=0, episode_number=n)
        library.rows["episodes"].append(relation(ep, "episode"))
    assert library.run("generate_series")["episodes_created"] == 2
    library.calls.clear()
    library.state["snapshot"] = Snapshot([owned(show, "series", [(0, 1)])])
    result = library.run(
        "generate_series", media_library_enabled=True, media_tv_mode=mode
    )
    assert result["reconciliation"]["deleted"] == 2 - remaining
    assert (
        len(list(Path(library.settings["series_root_folder"]).rglob("*.strm")))
        == remaining
    )
    if mode == "show":
        assert not library.calls  # no provider episode fetch for an owned show
    else:
        library.rows["episodes"].append(
            relation(
                media(3, series=show, season_number=0, episode_number=3), "episode"
            )
        )
        result = library.run(
            "generate_series", media_library_enabled=True, media_tv_mode=mode
        )
        assert result["episodes_created"] == 1


@pytest.mark.parametrize(
    "policy,status,created", [("continue", "ok", 1), ("stop", "error", 0)]
)
def test_failure_policy_no_server_deletion(library, policy, status, created):
    library.rows["movies"].append(relation(media(1)))
    library.run()
    library.rows["movies"].append(relation(media(2)))
    library.state["error"] = RuntimeError("Offline")
    result = library.run(media_library_enabled=True, media_server_failure=policy)
    assert result["status"] == status
    assert (
        len(list(Path(library.settings["root_folder"]).rglob("*.strm"))) == 1 + created
    )
    if policy == "continue":
        assert "WARNING" in result["message"]
        assert result["reconciliation"]["deleted"] == 0


def test_selected_libraries_and_shared_full_rescan_snapshot(library):
    result = library.run(
        "rescan_all",
        media_library_enabled=True,
        media_library_ids="a, b",
    )
    assert result["status"] == "ok"
    assert library.state["requests"] == [(["a", "b"], ["movie", "series"])]
    result = library.run(
        media_library_enabled=True,
        media_library_ids="",
        media_server_failure="stop",
    )
    assert result["status"] == "error"


@pytest.mark.parametrize(
    "timing,action,deletions",
    [
        ("disabled", "generate_movies", 0),
        ("rescan", "generate_movies", 0),
        ("rescan", "rescan_all", 1),
    ],
)
def test_duplicate_cleanup_timing(library, timing, action, deletions):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    library.state["snapshot"] = Snapshot([owned(obj)])
    result = library.run(
        action, media_library_enabled=True, media_duplicate_cleanup=timing
    )
    assert result["reconciliation"]["deleted"] == deletions
    assert result.get("created_strm", 0) == 0


def test_preview_then_selective_cleanup(library):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    library.state["snapshot"] = Snapshot([owned(obj)])
    result = library.run("preview_cleanup", media_library_enabled=True)
    assert result["reconciliation"]["candidate"] == 1
    assert list(Path(library.settings["root_folder"]).rglob("*.strm"))
    result = library.run("selective_cleanup", media_library_enabled=True)
    assert result["reconciliation"]["deleted"] == 1


def test_immediate_m3u_removal_reappearance_uuid_change(library):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    # UUID changes but account/provider source ID stays live.
    obj.uuid = "replacement-uuid"
    assert (
        library.run("selective_cleanup", m3u_cleanup_enabled=True)["reconciliation"][
            "deleted"
        ]
        == 0
    )
    library.rows["movies"].clear()
    result = library.run("selective_cleanup", m3u_cleanup_enabled=True)
    assert result["reconciliation"]["deleted"] == 1
    library.rows["movies"].append(relation(obj))
    assert library.run()["created_strm"] == 1


def test_m3u_ignores_category_and_generation_batch(library):
    library.rows["movies"].extend([relation(media(1)), relation(media(2))])
    library.run()
    library.rows["movies"].pop()
    result = library.run("rescan_all", m3u_cleanup_enabled=True, batch_size="1")
    assert result["reconciliation"]["deleted"] == 1
    assert len(list(Path(library.settings["root_folder"]).rglob("*.strm"))) == 1


def test_m3u_manual_timing(library):
    library.rows["movies"].append(relation(media(1)))
    library.run()
    library.rows["movies"].clear()
    assert (
        library.run(
            "rescan_all", m3u_cleanup_enabled=True, m3u_cleanup_timing="manual"
        )["reconciliation"]["deleted"]
        == 0
    )
    assert (
        library.run(
            "selective_cleanup", m3u_cleanup_enabled=True, m3u_cleanup_timing="manual"
        )["reconciliation"]["deleted"]
        == 1
    )


def test_failed_database_source_census_preserves_files(library, monkeypatch):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    library.rows["episodes"].append(
        relation(media(1, series=show, season_number=1, episode_number=1), "episode")
    )
    library.run("generate_series")
    library.rows["episodes"].clear()
    monkeypatch.setattr(
        Reconciliation,
        "tracked_source_census",
        lambda *a: (_ for _ in ()).throw(RuntimeError("Incomplete")),
    )
    result = library.run("selective_cleanup", m3u_cleanup_enabled=True)
    assert result["reconciliation"]["deleted"] == 0
    assert result["reconciliation"]["warnings"]
    assert list(Path(library.settings["series_root_folder"]).rglob("*.strm"))


def test_server_failure_and_m3u_cleanup_independent(library):
    library.rows["movies"].append(relation(media(1)))
    library.run()
    library.rows["movies"].clear()
    library.state["error"] = RuntimeError("Offline")
    result = library.run(
        "selective_cleanup", media_library_enabled=True, m3u_cleanup_enabled=True
    )
    assert result["reconciliation"]["deleted"] == 1
    assert result["reconciliation"]["warnings"]


def test_legacy_adoption_and_ambiguous_preservation(library):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    folder, filename, _, _ = library.p._movie_target_paths(
        obj, library.settings["root_folder"]
    )
    Path(folder).mkdir(parents=True)
    strm = Path(folder) / filename
    strm.write_text(
        library.p._build_proxy_url(
            library.settings["dispatcharr_url"], "movie", obj.uuid, "1"
        )
    )
    nfo = strm.with_suffix(".nfo")
    nfo.write_text("<movie><title>Custom</title></movie>")
    other = Path(folder) / "user.strm"
    other.write_text("http://unrelated/video")
    library.state["snapshot"] = Snapshot([owned(obj)])
    result = library.run(
        "selective_cleanup", media_library_enabled=True, deletion_scope="strm_nfo"
    )
    assert result["reconciliation"]["deleted"] == 1
    assert nfo.exists() and other.exists()
    assert result["reconciliation"]["preserved"] == 1


def test_edited_nfo_and_artwork_preserved(library):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    strm = next(Path(library.settings["root_folder"]).rglob("*.strm"))
    nfo = strm.with_suffix(".nfo")
    nfo.write_text("Edited")
    artwork = strm.parent / "poster.jpg"
    artwork.write_bytes(b"picture")
    subtitle = strm.parent / "movie.srt"
    subtitle.write_text("subtitle")
    library.state["snapshot"] = Snapshot([owned(obj)])
    result = library.run(media_library_enabled=True, deletion_scope="strm_nfo")
    assert result["reconciliation"]["deleted"] == 1
    assert nfo.read_text() == "Edited" and artwork.exists() and subtitle.exists()


def test_unchanged_generated_nfo_deleted(library):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    library.state["snapshot"] = Snapshot([owned(obj)])
    library.run(media_library_enabled=True, deletion_scope="strm_nfo")
    assert not list(Path(library.settings["root_folder"]).rglob("*.nfo"))


def test_edited_strm_preserved_even_full_refresh(library):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    strm = next(Path(library.settings["root_folder"]).rglob("*.strm"))
    strm.write_text("http://user-custom/video")
    library.state["snapshot"] = Snapshot([owned(obj)])
    result = library.run("rescan_all", media_library_enabled=True)
    assert result["reconciliation"]["preserved"] == 1
    assert strm.read_text() == "http://user-custom/video"
    library.state["snapshot"] = Snapshot([])
    library.run("rescan_all", media_library_enabled=True)
    assert strm.read_text() == "http://user-custom/video"


def test_schema_persistence_missing_files_and_rollback(tmp_path):
    store = InventoryStore(tmp_path / "state")
    file = tmp_path / "movie.strm"
    file.write_text("url")
    identity = Identity("movie", "A", 2000, "1")
    store.record(str(file), identity, "source")
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
    store.close()
    store = InventoryStore(tmp_path / "state")
    assert list(store.rows())[0]["tmdb"] == "1"
    with pytest.raises(sqlite3.IntegrityError):
        with store.db:
            store.db.execute("DELETE FROM files")
            store.db.execute("INSERT INTO files(path) VALUES (?)", ("invalid",))
    assert len(list(store.rows())) == 1
    file.unlink()
    assert store.delete(list(store.rows())[0], [str(tmp_path)]) == "missing"
    assert not list(store.rows())
    store.close()


def test_incremental_movies_skip_filesystem_and_only_hydrate_changes(library, monkeypatch):
    library.rows['movies'].extend(relation(media(n)) for n in range(1, 4))
    assert library.run()['created_strm'] == 3
    original_exists, original_lexists = os.path.exists, os.path.lexists

    def guard(original):
        def checked(path):
            assert not str(path).endswith('.strm'), 'Unchanged STRM was checked'
            return original(path)
        return checked

    with monkeypatch.context() as m:
        m.setattr(os.path, 'exists', guard(original_exists))
        m.setattr(os.path, 'lexists', guard(original_lexists))
        result = library.run()
    assert result['created_strm'] == 0
    assert result['scanned'] == 0
    assert result['unchanged_candidates'] == 3
    library.rows['movies'].append(relation(media(4)))
    result = library.run()
    assert result['created_strm'] == result['scanned'] == 1
    assert result['unchanged_candidates'] == 3


def test_incremental_source_changes_settings_and_relation_replacement(library):
    rel = relation(media(1))
    library.rows['movies'].append(rel)
    library.run()
    rel.id += 10000  # Sync replaced the Django row, same stable source.
    assert library.run()['scanned'] == 0
    rel.stream_id = 'new-provider-stream'
    result = library.run()
    assert result['refreshed_strm'] == 1
    path = next(Path(library.settings['root_folder']).rglob('*.strm'))
    assert 'new-provider-stream' in path.read_text()
    result = library.run(omit_stream_id=True)
    assert result['refreshed_strm'] == 1
    assert 'stream_id' not in path.read_text()


def test_incremental_batches_do_not_checkpoint_unprocessed_candidates(library):
    library.rows['movies'].extend(relation(media(n)) for n in range(1, 4))
    for _ in range(3):
        assert library.run(batch_size='1')['created_strm'] == 1
    assert library.run(batch_size='1')['scanned'] == 0


def test_incremental_relative_root_deletion_invalidates_cached_output(library, monkeypatch):
    monkeypatch.chdir(library.tmp)
    library.settings['root_folder'] = 'Movies'
    rel = relation(media(1))
    library.rows['movies'].append(rel)
    assert library.run()['created_strm'] == 1
    store = InventoryStore(library.tmp / 'state')
    path = list(store.rows())[0]['path']
    assert store.db.execute("select path from generation_entries where kind='movie'").fetchone()[0] == path
    with store.db:
        store.db.execute("UPDATE generation_entries SET path=?", (os.path.relpath(path),))
        store.db.execute("DELETE FROM generation_state WHERE key='absolute_paths'")
    store.close()
    store = InventoryStore(library.tmp / 'state')
    assert store.db.execute("select path from generation_entries where kind='movie'").fetchone()[0] == path
    store.close()
    library.rows['movies'].clear()
    assert library.run('selective_cleanup', m3u_cleanup_enabled=True)['reconciliation']['deleted'] == 1
    library.rows['movies'].append(rel)
    assert library.run()['created_strm'] == 1


def test_incremental_projection_crosses_lookup_and_write_batches(library):
    library.rows['movies'].extend(relation(media(n)) for n in range(1, BATCH_SIZE + 3))
    result = library.run(generate_nfo=False)
    assert result['created_strm'] == BATCH_SIZE + 2
    result = library.run(generate_nfo=False)
    assert result['scanned'] == 0
    assert result['unchanged_candidates'] == BATCH_SIZE + 2
    assert result['reconciliation']['timings']['incremental_movie_read']['items'] == BATCH_SIZE + 2


def test_external_removal_requires_rebuild_and_missing_root_invalidates_cache(library):
    library.rows['movies'].append(relation(media(1)))
    library.run()
    path = next(Path(library.settings['root_folder']).rglob('*.strm'))
    path.unlink()
    assert library.run()['created_strm'] == 0
    assert library.run('rebuild_inventory')['status'] == 'ok'
    assert library.run()['created_strm'] == 1
    import shutil
    shutil.rmtree(library.settings['root_folder'])
    assert library.run()['created_strm'] == 1


def test_incremental_failed_write_is_retried(library, monkeypatch):
    library.rows['movies'].append(relation(media(1)))
    original = library.p._write_if_different_preserve_times
    def failure(*args):
        raise OSError('test write failure')
    monkeypatch.setattr(library.p, '_write_if_different_preserve_times', failure)
    assert library.run()['errors'] == 1
    monkeypatch.setattr(library.p, '_write_if_different_preserve_times', original)
    assert library.run()['created_strm'] == 1


def test_incremental_ownership_write_failure_does_not_checkpoint(library, monkeypatch):
    library.rows['movies'].append(relation(media(1)))
    original = InventoryStore.record_many
    def failure(*args):
        raise sqlite3.OperationalError('test transaction failure')
    with monkeypatch.context() as m:
        m.setattr(InventoryStore, 'record_many', failure)
        assert library.run()['status'] == 'error'
    result = library.run()
    assert result['scanned'] == 1
    store = InventoryStore(library.tmp / 'state')
    assert len(list(store.rows())) == 1
    store.close()


def test_incremental_upgrade_seeds_verified_inventory_without_file_reads(library, monkeypatch):
    library.rows['movies'].append(relation(media(1)))
    library.run()
    store = InventoryStore(library.tmp / 'state')
    with store.db:
        store.db.execute('DELETE FROM generation_entries')
        store.db.execute('DELETE FROM generation_state')
        store.db.execute('PRAGMA user_version=2')
    store.close()
    original = os.path.exists
    def guard(path):
        assert not str(path).endswith('.strm')
        return original(path)
    with monkeypatch.context() as m:
        m.setattr(os.path, 'exists', guard)
        result = library.run()
    assert result['scanned'] == 0
    assert result['unchanged_candidates'] == 1


def test_incremental_episode_outputs_use_database_and_skip_unchanged(library, monkeypatch):
    show = media(10)
    library.rows['series'].append(relation(show, 'series'))
    library.rows['episodes'].append(relation(media(1, series=show, season_number=1, episode_number=1), 'episode'))
    assert library.run('generate_series', refresh_existing=True)['episodes_created'] == 1
    before = len(library.calls)
    original = os.path.isfile
    def guard(path):
        assert not str(path).endswith('.strm'), 'Unchanged episode was checked'
        return original(path)
    with monkeypatch.context() as m:
        m.setattr(os.path, 'isfile', guard)
        result = library.run('generate_series', refresh_existing=True)
    assert len(library.calls) == before
    assert result['episodes_created'] == 0
    assert result['reconciliation']['generation_unchanged'] == 1
    library.rows['episodes'].append(relation(media(2, series=show, season_number=1, episode_number=2), 'episode'))
    assert library.run('generate_series', refresh_existing=True)['episodes_created'] == 1
def test_path_containment(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "media-other"
    outside.mkdir()
    file = outside / "movie.strm"
    file.write_text("url")
    store = InventoryStore(tmp_path / "state")
    store.record(str(file), Identity("movie", "A", 2000))
    assert store.delete(list(store.rows())[0], [str(root)]) == "preserved"
    assert not contained(str(root / ".." / "media-other" / "movie.strm"), [str(root)])
    assert file.exists()
    store.close()


def test_symlink_containment(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable on this Windows host")
    assert not contained(str(root / "linked" / "movie.strm"), [str(root)])


def test_process_lock(tmp_path):
    with action_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Another"):
            with action_lock(tmp_path):
                pass
    with action_lock(tmp_path):
        pass


@pytest.mark.parametrize(
    "candidate,expected",
    [
        (Identity("movie", "Different title", None, "1"), True),
        (Identity("series", "A", 2000, "1"), False),
        (Identity("movie", "A", 2000, "2"), False),
        (Identity("movie", "A", 2000, "1", "tt2"), False),
        (Identity("movie", "A", 2000), True),
        (Identity("movie", "A", None), False),
        (Identity("movie", "A", 2001), False),
        (Identity("movie", "A extended", 2000), False),
    ],
)
def test_matching_rules(candidate, expected):
    snapshot = Snapshot([OwnedMedia(Identity("movie", "A", 2000, "1", "tt1"))])
    assert snapshot.owns(candidate) == expected


def test_ids_unavailable_fallback_clean_title():
    p = Plugin()
    snapshot = Snapshot(
        [OwnedMedia(Identity("movie", "A", 2000, imdb="tt1"))],
        lambda title: p._extract_clean_name_and_year(title)[0],
    )
    assert snapshot.owns(Identity("movie", "EN - A 4K", 2000, tmdb="1"))
    assert snapshot.text is not None


def test_id_matching_does_not_build_title_index():
    snapshot = Snapshot([OwnedMedia(Identity("movie", "A", 2000, "1"))])
    assert snapshot.owns(Identity("movie", "A", 2000, "1"))
    assert snapshot.text is None


def test_adapter_paginated_snapshot_mixed_sources_and_specials(monkeypatch):
    adapter = EmbyAdapter("http://emby", "secret")
    adapter.PAGE_SIZE = 2
    items = [
        {
            "Id": "s",
            "Type": "Series",
            "Name": "Show",
            "ProductionYear": 2000,
            "ProviderIds": {"Tmdb": "10"},
        },
        {
            "Id": "e",
            "Type": "Episode",
            "SeriesId": "s",
            "ParentIndexNumber": 0,
            "IndexNumber": 1,
            "IndexNumberEnd": 2,
            "Path": "/real.mkv",
        },
        {"Id": "strm", "Type": "Movie", "Name": "Fake", "Path": "/fake.strm"},
        {
            "Id": "mixed",
            "Type": "Movie",
            "Name": "Movie",
            "ProviderIds": {"Imdb": "tt1"},
            "MediaSources": [
                {"Path": "/a.strm"},
                {"Path": "/a.mkv", "Protocol": "File"},
            ],
        },
        {
            "Id": "remote",
            "Type": "Movie",
            "Name": "Remote",
            "Path": "http://example/video.mp4",
        },
    ]
    requests = []

    def get(path, params):
        requests.append(params)
        return {
            "Items": items[
                params["StartIndex"] : params["StartIndex"] + params["Limit"]
            ],
            "TotalRecordCount": len(items)
            if params["EnableTotalRecordCount"] == "true"
            else 0,
        }

    monkeypatch.setattr(adapter, "_get", get)
    snapshot = adapter.get_snapshot(["library"], ["movie", "series"])
    assert len(requests) == 4
    assert all(
        r["ParentId"] == "library"
        and r["EnableImages"] == "false"
        and r["EnableUserData"] == "false"
        for r in requests
    )
    assert len(snapshot.media) == 2
    show = Identity("series", "Show", 2000, "10")
    assert snapshot.owns(show, (0, 2), "episodes")
    assert not snapshot.owns(show, (0, 3), "episodes")


@pytest.mark.parametrize("failure", ["short", "repeated", "changed", "invalid"])
def test_adapter_discards_incomplete_snapshot(monkeypatch, failure):
    adapter = EmbyAdapter("http://emby", "secret")
    adapter.PAGE_SIZE = 1

    def get(path, params):
        if params["Limit"] == 0:
            return {"Items": [], "TotalRecordCount": 1 if failure == "changed" else 2}
        if params["StartIndex"] == 0:
            return {
                "Items": [{"Id": "a", "Type": "Movie", "Path": "/a.mkv"}],
                "TotalRecordCount": 2,
            }
        if failure == "short":
            return {"Items": [], "TotalRecordCount": 0}
        if failure == "repeated":
            return {"Items": [{"Id": "a"}], "TotalRecordCount": 0}
        if failure == "changed":
            return {
                "Items": [{"Id": "b", "Type": "Movie", "Path": "/b.mkv"}],
                "TotalRecordCount": 0,
            }
        return {"Items": None, "TotalRecordCount": 0}

    monkeypatch.setattr(adapter, "_get", get)
    with pytest.raises(ValueError):
        adapter.get_snapshot(["library"], ["movie"])


def test_adapter_http_header_and_timeout(monkeypatch):
    requests = []

    class Response(io.BytesIO):
        pass

    def open(request, timeout):
        requests.append((request, timeout))
        return Response(b'{"Items":[{"Id":"1","Name":"Movies"}]}')

    monkeypatch.setattr("media_library.urlopen", open)
    adapter = EmbyAdapter("http://emby/emby", "secret")
    assert adapter.list_libraries()[0]["Id"] == "1"
    request, timeout = requests[0]
    assert request.get_header("X-emby-token") == "secret" and timeout == 30
    assert "secret" not in request.full_url


def test_http_failure_does_not_leak_token(monkeypatch):
    monkeypatch.setattr(
        "media_library.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    with pytest.raises(RuntimeError) as exc:
        EmbyAdapter("http://emby", "secret").list_libraries()
    assert "secret" not in str(exc.value)


def test_runtime_manifest_agreement_and_schedule_snapshot():
    manifest = json.loads(Path("plugin.json").read_text(encoding="utf8"))
    assert [
        (f["id"], f.get("default"), f.get("options")) for f in manifest["fields"]
    ] == [(f["id"], f.get("default"), f.get("options")) for f in Plugin.fields]
    assert [a["id"] for a in manifest["actions"]] == [a["id"] for a in Plugin.actions]
    assert manifest["version"] == Plugin.version
    action_ids = {a["id"] for a in manifest["actions"]}
    assert not {"apply_schedule", "remove_schedule"} & action_ids
    assert next(f for f in manifest["fields"] if f["id"] == "schedule_enabled")["default"] is False
    fields = {f["id"] for f in Plugin.fields}
    settings = {f["id"]: f.get("default") for f in Plugin.fields if "default" in f}
    snapshot = {k: v for k, v in settings.items() if not k.startswith("schedule_")}
    assert {
        "media_library_enabled",
        "media_server_token",
        "m3u_cleanup_enabled",
        "deletion_scope",
    } <= fields & snapshot.keys()


def test_movie_inventory_flushes_bulk_batches(library, monkeypatch):
    sizes = []
    original = InventoryStore.record_many

    def record_many(store, records):
        sizes.append(len(records))
        return original(store, records)

    monkeypatch.setattr(InventoryStore, "record_many", record_many)
    library.rows["movies"].extend(relation(media(i)) for i in range(1, BATCH_SIZE + 2))
    assert library.run(generate_nfo=False)["created_strm"] == BATCH_SIZE + 1
    assert sizes == [BATCH_SIZE, 1]


def test_episode_queue_bounded_without_deadlock(library):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    library.rows["episodes"].extend(
        relation(media(i, series=show, season_number=1, episode_number=i), "episode")
        for i in range(1, BATCH_SIZE + 102)
    )
    result = library.run("generate_series", generate_series_nfo=False)
    assert result["episodes_created"] == BATCH_SIZE + 101
    store = InventoryStore(library.tmp / "state")
    assert len(list(store.rows())) == BATCH_SIZE + 101
    store.close()


def test_record_many_uses_executemany(tmp_path):
    store = InventoryStore(tmp_path / "state")
    calls = []

    class Proxy:
        def __init__(self, db):
            self.db = db

        def execute(self, *args):
            return self.db.execute(*args)

        def executemany(self, sql, rows):
            calls.append((sql, len(rows)))
            return self.db.executemany(sql, rows)

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *args):
            return self.db.__exit__(*args)

        def close(self):
            return self.db.close()

    store.db = Proxy(store.db)
    records = []
    for i in range(3):
        path = tmp_path / f"{i}.strm"
        path.write_text("url")
        records.append((str(path), Identity("movie", str(i), 2000), str(i), None, None))
    store.record_many(records)
    assert len(calls) == 2 and all(count == 3 for _, count in calls)
    store.close()


def test_m3u_removed_show_with_orphan_episode_rows(library):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    library.rows["episodes"].append(
        relation(media(1, series=show, season_number=1, episode_number=1), "episode")
    )
    assert library.run("generate_series")["episodes_created"] == 1
    library.rows["series"].clear()
    result = library.run("selective_cleanup", m3u_cleanup_enabled=True)
    assert result["reconciliation"]["deleted"] == 1


def test_m3u_episode_removal_and_reappearance(library, monkeypatch):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    ep1 = relation(media(1, series=show, season_number=1, episode_number=1), "episode")
    ep2 = relation(media(2, series=show, season_number=1, episode_number=2), "episode")
    library.rows["episodes"].extend([ep1, ep2])
    library.run("generate_series")

    library.rows["episodes"][:] = [ep1]
    result = library.run("selective_cleanup", m3u_cleanup_enabled=True)
    assert result["reconciliation"]["deleted"] == 1
    library.rows["episodes"].append(ep2)
    result = library.run("generate_series", refresh_existing=True)
    assert result["episodes_created"] == 1




def test_m3u_reuses_complete_census_when_no_episode_query(library, monkeypatch):
    library.rows["movies"].append(relation(media(1)))
    library.run()
    library.run('preview_cleanup')  # Establish the generated root marker.
    passes = []
    census = Reconciliation.census

    def counted(self, **kwargs):
        passes.append(True)
        return census(self, **kwargs)

    monkeypatch.setattr(Reconciliation, "census", counted)
    library.rows["movies"].clear()
    result = library.run("selective_cleanup", m3u_cleanup_enabled=True)
    assert passes == [True]
    assert result["reconciliation"]["deleted"] == 1


def test_bulk_listing_matches_separate_strm_and_file_versions(monkeypatch):
    adapter = EmbyAdapter("http://emby", "secret")
    versions = [
        {
            "Id": "strm",
            "Type": "Movie",
            "Name": "Movie",
            "ProviderIds": {"Tmdb": "1"},
            "Path": "/movie.strm",
        },
        {
            "Id": "file",
            "Type": "Movie",
            "Name": "Movie",
            "ProviderIds": {"Tmdb": "1"},
            "Path": "/movie.mkv",
        },
    ]

    def get(path, params):
        return {"Items": versions if params["Limit"] else [], "TotalRecordCount": 2}

    monkeypatch.setattr(adapter, "_get", get)
    snapshot = adapter.get_snapshot(["library"], ["movie"])
    assert len(snapshot.media) == 1
    assert snapshot.owns(Identity("movie", "Movie", tmdb="1"))




def test_unknown_schema_is_rejected(tmp_path):
    store = InventoryStore(tmp_path)
    store.db.execute("PRAGMA user_version=99")
    store.close()
    with pytest.raises(ValueError, match="Unsupported"):
        InventoryStore(tmp_path)


def test_generated_nfo_is_preserved_in_archive_by_default(library):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    before = next(Path(library.settings['root_folder']).rglob('*.nfo')).read_bytes()
    library.state["snapshot"] = Snapshot([owned(obj)])
    library.run(media_library_enabled=True)
    assert not list(Path(library.settings['root_folder']).rglob('*.nfo'))
    assert next((library.tmp / 'state' / 'filtered-nfo').rglob('*.nfo')).read_bytes() == before


def test_edited_generated_nfo_shared_show_cleanup(library):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    library.rows["episodes"].extend(
        relation(media(i, series=show, season_number=1, episode_number=i), "episode")
        for i in (1, 2)
    )
    library.run("generate_series")
    library.state["snapshot"] = Snapshot([owned(show, "series", [(1, 1)])])
    library.run(
        "generate_series",
        media_library_enabled=True,
        media_tv_mode="episodes",
        deletion_scope="strm_nfo",
    )
    root = Path(library.settings["series_root_folder"])
    assert len(list(root.rglob("tvshow.nfo"))) == 1
    assert len(list(root.rglob("*.nfo"))) == 2  # shared show and one retained episode
    library.run(
        "generate_series",
        media_library_enabled=True,
        media_tv_mode="show",
        deletion_scope="strm_nfo",
    )
    assert not list(root.rglob("*.nfo"))


def test_strm_inventory_stores_raw_url_without_hash(library, monkeypatch):
    # Movie generation with NFOs off must not call the hash helper at all.
    def unexpected_hash(*args):
        raise AssertionError("STRMs should use URL equality")

    monkeypatch.setattr("plugin.file_hash", unexpected_hash)
    monkeypatch.setattr("inventory.file_hash", unexpected_hash)
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    assert library.run(generate_nfo=False)["created_strm"] == 1
    store = InventoryStore(library.tmp / "state")
    row = next(iter(store.rows()))
    assert row["strm_url"] == library.p._build_proxy_url(
        library.settings["dispatcharr_url"], "movie", obj.uuid, "1"
    )
    assert "strm_hash" not in row.keys()
    store.close()
    library.state["snapshot"] = Snapshot([owned(obj)])
    assert (
        library.run(media_library_enabled=True, generate_nfo=False)["reconciliation"][
            "deleted"
        ]
        == 1
    )


def test_whole_show_mode_never_fetches_owned_provider_episodes(library, monkeypatch):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    library.rows["episodes"].append(
        relation(media(1, series=show, season_number=1, episode_number=1), "episode")
    )
    library.run("generate_series")
    library.state["snapshot"] = Snapshot([owned(show, "series", [(1, 1)])])

    def forbidden(*args):
        raise AssertionError("Owned shows must be excluded before fetching episodes")

    monkeypatch.setitem(sys.modules, "core.xtream_codes", NS(Client=forbidden))
    result = library.run(
        "rescan_all", media_library_enabled=True, m3u_cleanup_enabled=True
    )
    assert result["reconciliation"]["deleted"] == 1
    assert not result["reconciliation"]["warnings"]


def test_existing_binary_nfo_does_not_prevent_episode_generation(library):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    library.rows["episodes"].append(
        relation(media(1, series=show, season_number=1, episode_number=1), "episode")
    )
    folder, _, _ = library.p._series_target_folder(
        show, library.settings["series_root_folder"]
    )
    Path(folder).mkdir(parents=True)
    (Path(folder) / "tvshow.nfo").write_bytes(b"\xff\xfeCustom")
    result = library.run("generate_series")
    assert result["episodes_created"] == 1
    assert (Path(folder) / "tvshow.nfo").read_bytes() == b"\xff\xfeCustom"


def test_sidecar_deletion_failure_is_reported_and_retryable(library, monkeypatch):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    library.state["snapshot"] = Snapshot([owned(obj)])
    real_remove = os.remove

    def remove(path):
        if str(path).endswith(".nfo"):
            raise PermissionError("Sidecar is read-only")
        return real_remove(path)

    monkeypatch.setattr("inventory.os.remove", remove)
    result = library.run(media_library_enabled=True, deletion_scope="strm_nfo")
    assert result["reconciliation"]["deleted"] == 1
    assert result["reconciliation"]["errors"] == 1
    monkeypatch.setattr("inventory.os.remove", real_remove)
    result = library.run(media_library_enabled=True, deletion_scope="strm_nfo")
    assert result["reconciliation"]["deleted_nfo"] == 1
    assert not list(Path(library.settings["root_folder"]).rglob("*.nfo"))


def test_inventory_failure_cancels_bounded_workers(library, monkeypatch):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    library.rows["episodes"].extend(
        relation(media(i, series=show, season_number=1, episode_number=i), "episode")
        for i in range(1, BATCH_SIZE + 102)
    )

    def failure(*args):
        raise sqlite3.OperationalError("Disk full")

    monkeypatch.setattr(InventoryStore, "record_many", failure)
    result = library.run("generate_series", generate_series_nfo=False)
    assert result["status"] == "error"
    assert "Disk full" in result["message"]


@pytest.mark.parametrize("policy", ["continue", "stop"])
@pytest.mark.parametrize("selection", ["", " , ", "Missing", "stale-id"])
def test_explicit_library_selection_required_before_file_changes(library, policy, selection):
    library.rows["movies"].append(relation(media(1)))
    result = library.run(
        media_library_enabled=True, media_library_ids=selection,
        media_library_scope="all", media_server_failure=policy,
    )
    assert result["status"] == "error"
    assert library.state["requests"] == []
    assert not list(library.tmp.rglob("*.strm"))


def test_library_names_normalized_and_resolved_each_action(library, monkeypatch):
    from media_library import LibrarySelectionError, resolve_library_ids
    selections = ["Movies", "tv shows"]
    initial = [{"Id": "1", "Name": " Movies "}, {"Id": "2", "Name": "TV Shows"}]
    assert resolve_library_ids(initial, selections) == ["1", "2"]
    recreated = [{"Id": "3", "Name": "MOVIES"}, {"Id": "4", "Name": "TV SHOWS"}]
    assert resolve_library_ids(recreated, selections) == ["3", "4"]
    assert resolve_library_ids(recreated, [" 3 ", " movies "]) == ["3"]
    duplicates = recreated + [{"Id": "5", "Name": "Movies"}]
    with pytest.raises(LibrarySelectionError, match="ambiguous"):
        resolve_library_ids(duplicates, ["movies"])
    assert resolve_library_ids(duplicates, ["3"]) == ["3"]
    result = library.run(
        media_library_enabled=True, media_library_ids="  mOViEs , TV SHOWS, library  ",
    )
    assert result["status"] == "ok"
    assert library.state["requests"] == [(["library", "a"], ["movie", "series"])]
    assert "media_library_scope" not in {field["id"] for field in Plugin.fields}


def test_adapter_never_falls_back_to_global_query(monkeypatch):
    adapter = EmbyAdapter("http://emby", "secret")
    monkeypatch.setattr(adapter, "_get", lambda *_: pytest.fail("No global requests"))
    with pytest.raises(ValueError, match="explicit"):
        adapter.get_snapshot([], ["movie"])


def test_census_preserves_positions_sources_and_account_membership(library, monkeypatch):
    from reconciliation import identity_for, source_for
    show = media(10, name="Show (2000)")
    library.rows["series"].append(relation(show, "series"))
    episodes = [relation(media(n, series=show, season_number=0, episode_number=n), "episode")
                for n in (1, 2)]
    orphan = relation(media(3, series=show, season_number=1, episode_number=3), "episode")
    orphan.m3u_account_id = 2
    library.rows["episodes"].extend(episodes + [orphan])
    rec = Reconciliation(library.p, library.settings, LOG, library.tmp / "census")
    calls = []
    clean = library.p._extract_clean_name_and_year
    monkeypatch.setattr(library.p, "_extract_clean_name_and_year",
                        lambda name: (calls.append(name), clean(name))[1])
    try:
        rec.census()
        assert calls == [show.name]
        rows = list(rec.store.db.execute("SELECT * FROM catalogue ORDER BY episode"))
        assert len(rows) == 2
        for row, episode in zip(rows, episodes):
            assert row["uuid"] == episode.episode.uuid
            assert row["source"] == source_for(episode, "episode")
            assert (row["season"], row["episode"]) == (0, episode.episode.episode_number)
            assert Identity(**json.loads(row["identity"])) == identity_for(library.p, show, "series")
    finally:
        rec.store.close()


def test_timing_metrics_account_for_batches_and_failed_phases(library, monkeypatch, caplog):
    rec = Reconciliation(library.p, library.settings, LOG, library.tmp / "timing")
    wall, cpu = iter([10.0, 12.0]), iter([2.0, 2.5])
    monkeypatch.setattr("reconciliation.time.perf_counter", lambda: next(wall))
    monkeypatch.setattr("reconciliation.time.process_time", lambda: next(cpu))
    try:
        with pytest.raises(RuntimeError, match="Failure"):
            with rec.measure("failed_phase", 17):
                raise RuntimeError("Failure")
        assert rec.report["timings"]["failed_phase"] == {
            "wall_seconds": 2.0, "cpu_seconds": 0.5, "calls": 1, "items": 17,
        }
    finally:
        rec.store.close()


def test_action_telemetry_survives_status_result_and_logs_no_settings(library, caplog):
    Path(library.settings["root_folder"]).mkdir()
    library.rows["movies"].append(relation(media(1)))
    with caplog.at_level(logging.INFO):
        result = library.run(media_library_enabled=True, media_server_token="private-key")
    report = result["reconciliation"]
    timings = report["timings"]
    assert report["worker_pid"] == os.getpid()
    assert timings["catalogue_movie_read"]["items"] == 1
    assert timings["catalogue_sqlite_write"]["items"] == 1
    assert timings["catalogue_processing"]["wall_seconds"] >= 0
    assert timings["total"]["wall_seconds"] >= timings["catalogue"]["wall_seconds"]
    assert "generate_movies" in timings and "emby_snapshot" in timings
    assert "VOD2MLIB timing summary" in caplog.text
    assert "private-key" not in caplog.text
    json.dumps(report)
    saved = json.loads((library.tmp / "state" / "timings.json").read_text(encoding="utf8"))
    assert saved["timings"] == timings
    assert "private-key" not in json.dumps(saved)
    if os.name == "posix":
        assert (library.tmp / "state" / "timings.json").stat().st_mode & 0o777 == 0o600


def test_preview_does_not_stat_non_candidates(library, monkeypatch):
    library.rows["movies"].append(relation(media(1)))
    library.run()
    rec = Reconciliation(library.p, library.settings, LOG, library.tmp / "state")
    monkeypatch.setattr("reconciliation.os.path.lexists",
                        lambda *_: pytest.fail("Preview statted a non-candidate"))
    try:
        rec.cleanup(False, False, True)
        assert rec.report["candidate"] == 0
    finally:
        rec.store.close()


def test_discovery_skips_symlink_files_and_directories(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir(); outside.mkdir()
    real = root / "nested"; real.mkdir()
    (real / "movie.strm").write_text("generated")
    (outside / "unrelated.strm").write_text("unrelated")
    try:
        (root / "linked-dir").symlink_to(outside, target_is_directory=True)
        (root / "linked-file.strm").symlink_to(outside / "unrelated.strm")
    except OSError:
        pytest.skip("Symlink creation unavailable")
    assert list(Reconciliation.generated_paths(root)) == [str(real / "movie.strm")]
    assert list(Reconciliation.generated_paths(tmp_path / "missing")) == []





def write_external_strm(library, obj, root=None):
    root = Path(root or library.settings["root_folder"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"external-{obj.id}.strm"
    path.write_text(library.p._build_proxy_url(
        library.settings["dispatcharr_url"], "movie", obj.uuid, str(obj.id),
    ), encoding="utf8")
    return path


def test_initial_discovery_then_external_addition_requires_rebuild(library):
    a, b = media(1), media(2)
    library.rows["movies"].extend([relation(a), relation(b)])
    first = write_external_strm(library, a)
    result = library.run("preview_cleanup")
    assert result["reconciliation"]["adopted"] == 1
    assert result["reconciliation"]["discovery_scanned_roots"] == 1
    second = write_external_strm(library, b)
    nfo = second.with_suffix(".nfo"); nfo.write_text("Custom NFO")
    result = library.run("preview_cleanup")
    assert result["reconciliation"]["discovery_skipped_roots"] == 1
    assert result["reconciliation"]["adopted"] == 0
    # Rebuild is independent of media-server configuration/failure and never deletes.
    library.state["error"] = RuntimeError("Offline")
    result = library.run("rebuild_inventory", media_library_enabled=True,
                         media_library_ids="", m3u_cleanup_enabled=True)
    assert result["status"] == "ok"
    assert result["reconciliation"]["adopted"] == 1
    assert result["reconciliation"]["deleted"] == 0
    assert first.exists() and second.exists() and nfo.read_text() == "Custom NFO"
    assert library.state["requests"] == []
    store = InventoryStore(library.tmp / "state")
    assert len(list(store.rows())) == 2
    assert all(json.loads(r["nfos"]) == {} for r in store.rows())
    store.close()


def test_rebuild_preserves_existing_ownership_and_generated_nfo_hashes(library):
    obj = media(1)
    library.rows["movies"].append(relation(obj))
    library.run()
    store = InventoryStore(library.tmp / "state")
    before = [tuple(row) for row in store.rows()]; store.close()
    nfo = next(library.tmp.rglob("*.nfo")); nfo.write_text("Edited metadata")
    strm = next(library.tmp.rglob("*.strm")); strm.write_text("http://user-custom/file")
    result = library.run("rebuild_inventory")
    assert result["status"] == "ok" and result["reconciliation"]["adopted"] == 0
    store = InventoryStore(library.tmp / "state")
    assert [tuple(row) for row in store.rows()] == before
    store.close()
    assert strm.read_text() == "http://user-custom/file" and nfo.read_text() == "Edited metadata"


def test_discovery_markers_survive_schema_one_upgrade(tmp_path):
    store = InventoryStore(tmp_path / "state")
    path = tmp_path / "movie.strm"; path.write_text("url")
    store.record(str(path), Identity("movie", "A", 2000, "1"), "source",
                 nfos={str(tmp_path / "movie.nfo"): "generated-hash"})
    before = [tuple(row) for row in store.rows()]
    store.db.execute("DROP TABLE discovery_roots")
    store.db.execute("PRAGMA user_version=1"); store.close()
    store = InventoryStore(tmp_path / "state")
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert [tuple(row) for row in store.rows()] == before
    store.mark_discovered("root", "context"); store.close()
    store = InventoryStore(tmp_path / "state")
    assert store.discovery_complete("root", "context")
    assert not store.discovery_complete("root", "new-context")
    store.close()


def test_changed_and_previously_missing_roots_discovered_automatically(library):
    obj = media(1); library.rows["movies"].append(relation(obj))
    library.run("preview_cleanup")  # Missing roots must not be marked complete.
    write_external_strm(library, obj)
    assert library.run("preview_cleanup")["reconciliation"]["adopted"] == 1
    other = library.tmp / "new-output"
    write_external_strm(library, obj, other)
    result = library.run("preview_cleanup", root_folder=str(other))
    assert result["reconciliation"]["adopted"] == 1
    assert result["reconciliation"]["discovery_scanned_roots"] == 1


@pytest.mark.parametrize("failure", ["traversal", "read"])
def test_failed_forced_discovery_invalidates_marker_and_retries(library, monkeypatch, failure):
    a, b = media(1), media(2)
    library.rows["movies"].extend([relation(a), relation(b)])
    write_external_strm(library, a); library.run("preview_cleanup")
    second = write_external_strm(library, b)
    with monkeypatch.context() as patch:
        if failure == "traversal":
            def failed(*_args, **_kwargs):
                raise PermissionError("Traversal failed")
            patch.setattr(Reconciliation, "generated_paths", staticmethod(failed))
        else:
            read = Path.read_text
            def failed(path, *args, **kwargs):
                if path == second:
                    raise PermissionError("Read failed")
                return read(path, *args, **kwargs)
            patch.setattr(Path, "read_text", failed)
        assert library.run("rebuild_inventory")["status"] == "error"
    result = library.run("preview_cleanup")
    assert result["reconciliation"]["discovery_scanned_roots"] == 1
    assert result["reconciliation"]["adopted"] == 1
    assert second.exists()



def test_lean_and_metadata_census_agree_on_sources_without_identity_work(library, monkeypatch):
    from reconciliation import source_for
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    a, b = relation(media(1)), relation(media(2))
    b.m3u_account_id = 2
    ep = relation(media(3, series=show, season_number=0, episode_number=1), "episode")
    orphan = relation(media(4, series=show, season_number=1, episode_number=2), "episode")
    orphan.m3u_account_id = 2
    library.rows["movies"].extend([a, b, a])  # Duplicate source across catalogue rows.
    library.rows["episodes"].extend([ep, orphan])
    rich = Reconciliation(library.p, library.settings, LOG, library.tmp / "rich")
    lean = Reconciliation(library.p, library.settings, LOG, library.tmp / "lean")
    try:
        rich.census()
        expected = {r[0] for r in rich.store.db.execute("SELECT source FROM live")}
        monkeypatch.setattr(library.p, "_extract_clean_name_and_year",
                            lambda *_: pytest.fail("Lean census cleaned metadata"))
        lean.source_census()
        assert {r[0] for r in lean.store.db.execute("SELECT source FROM live")} == expected
        assert source_for(orphan, "episode") not in expected
        assert lean.store.db.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0] == 0
        assert "catalogue_index_build" not in lean.report["timings"]
        assert lean.census_count == rich.census_count == 4
    finally:
        rich.store.close(); lean.store.close()


def test_routine_checks_skip_census_without_m3u_or_pending_discovery(library, monkeypatch):
    library.rows["movies"].append(relation(media(1)))
    write_external_strm(library, media(1))
    library.run("preview_cleanup")
    monkeypatch.setattr(Reconciliation, "census", lambda *_args, **_kw: pytest.fail("Unneeded census"))
    result = library.run("preview_cleanup", media_library_enabled=True)
    assert result["status"] == "ok"
    assert result["reconciliation"]["catalogue_modes"] == []
    assert "catalogue" not in result["reconciliation"]["timings"]


def test_routine_m3u_uses_sources_and_rebuild_uses_metadata(library):
    obj = media(1); library.rows["movies"].append(relation(obj))
    write_external_strm(library, obj)
    assert library.run("preview_cleanup")["reconciliation"]["catalogue_modes"] == ["metadata"]
    result = library.run("preview_cleanup", m3u_cleanup_enabled=True)
    assert result["reconciliation"]["catalogue_modes"] == ["sources"]
    assert library.run("rebuild_inventory")["reconciliation"]["catalogue_modes"] == ["metadata"]


def test_tracked_presence_matches_complete_census_for_all_managed_sources(library):
    from reconciliation import source_for, identity_for
    show = media(10)
    library.rows['series'].append(relation(show, 'series'))
    movie = relation(media(1), stream='shared-stream')
    other = relation(media(2), stream='shared-stream'); other.m3u_account_id = 2
    episode = relation(media(3, series=show, season_number=0, episode_number=1), 'episode', 'shared-stream')
    orphan = relation(media(4, series=show, season_number=1, episode_number=2), 'episode', 'orphan')
    orphan.m3u_account_id = 2
    missing = relation(media(5), stream='missing')
    library.rows['movies'].extend([movie, other, relation(media(99))])
    library.rows['episodes'].extend([episode, orphan, relation(media(98, series=show), 'episode')])
    full = Reconciliation(library.p, library.settings, LOG, library.tmp / 'full')
    targeted = Reconciliation(library.p, library.settings, LOG, library.tmp / 'targeted')
    try:
        full.source_census()
        expected = {r[0] for r in full.store.db.execute('SELECT source FROM live')}
        required = set()
        for i, (rel, kind) in enumerate([(movie, 'movie'), (other, 'movie'), (episode, 'episode'), (orphan, 'episode'), (missing, 'movie')]):
            source = source_for(rel, kind);required.add(source)
            path = library.tmp / f'presence-{i}.strm';path.write_text('url')
            obj = rel.movie if kind == 'movie' else show
            targeted.store.record(str(path), identity_for(library.p, obj, 'movie' if kind == 'movie' else 'series'), source)
        # Valid source aliases should all establish presence, rather than one
        # JSON spelling accidentally causing another spelling to be deleted.
        alias = json.dumps(json.loads(source_for(movie, 'movie')), separators=(',', ':'))
        required.add(alias)
        with targeted.store.db:
            targeted.store.db.execute('INSERT INTO sources VALUES (?,?)', ('alias-path', alias))
        targeted.tracked_source_census()
        found = {r[0] for r in targeted.store.db.execute('SELECT source FROM live')}
        assert found == (expected & required) | {alias}
        assert source_for(orphan, 'episode') not in found
        assert targeted.report['source_presence_requested'] == 6
        assert targeted.report['source_presence_found'] == 4
        assert targeted.store.db.execute('SELECT COUNT(*) FROM catalogue').fetchone()[0] == 0
    finally:
        full.store.close();targeted.store.close()


def test_tracked_presence_queries_only_required_kinds_and_bounds_sources(library, monkeypatch):
    library.rows['movies'].extend(relation(media(n)) for n in range(1, BATCH_SIZE + 3))
    library.run(generate_nfo=False)
    library.run('preview_cleanup')  # Establish the newly generated root marker.
    original = Query.filter
    calls = []
    def observed(self, **kwargs):
        if 'stream_id__in' in kwargs:
            calls.append(len(kwargs['stream_id__in']))
            assert len(kwargs['stream_id__in']) <= 900
        return original(self, **kwargs)
    monkeypatch.setattr(Query, 'filter', observed)
    result = library.run('preview_cleanup', m3u_cleanup_enabled=True, batch_size='1')
    report = result['reconciliation']
    assert report['source_presence_requested'] == BATCH_SIZE + 2
    assert report['source_presence_found'] == BATCH_SIZE + 2
    assert report['source_presence_queries'] == 2
    assert calls == [900, 102]
    assert 'catalogue_episode_read' not in report['timings']
    assert 'catalogue_series_read' not in report['timings']


def test_invalid_tracked_source_disables_m3u_deletion(library):
    library.rows['movies'].append(relation(media(1)))
    library.run()
    library.run('preview_cleanup')  # Discover the generated root before routine checks.
    store = InventoryStore(library.tmp / 'state')
    with store.db:store.db.execute("UPDATE sources SET source='unknown legacy source'")
    store.close()
    library.rows['movies'].clear()
    result = library.run('selective_cleanup', m3u_cleanup_enabled=True)
    assert result['reconciliation']['deleted'] == 0
    assert result['reconciliation']['warnings']
    assert len(list(Path(library.settings['root_folder']).rglob('*.strm'))) == 1


def test_failed_lean_query_never_establishes_m3u_absence(library, monkeypatch):
    obj = media(1); library.rows["movies"].append(relation(obj))
    strm = write_external_strm(library, obj)
    library.run("preview_cleanup")
    iterator = Query.iterator
    def fail(self, **kwargs):
        yield from iterator(self, **kwargs)
        raise RuntimeError("Incomplete catalogue")
    monkeypatch.setattr(Query, "iterator", fail)
    result = library.run("selective_cleanup", m3u_cleanup_enabled=True)
    assert result["reconciliation"]["deleted"] == 0
    assert result["reconciliation"]["warnings"] and strm.exists()


def test_cleanup_uses_database_source_census_once(library, monkeypatch):
    show = media(10)
    library.rows["series"].append(relation(show, "series"))
    library.rows["episodes"].append(
        relation(media(1, series=show, season_number=1, episode_number=1), "episode")
    )
    library.run("generate_series")
    result = library.run("preview_cleanup", m3u_cleanup_enabled=True)
    assert result["reconciliation"]["catalogue_modes"] == ["metadata", "sources"]


def test_forced_discovery_invalidates_marker_when_root_stat_fails(library, monkeypatch):
    obj = media(1); library.rows["movies"].append(relation(obj))
    write_external_strm(library, obj); library.run("preview_cleanup")
    root = library.settings["root_folder"]
    stat = os.stat
    with monkeypatch.context() as patch:
        def failed(path, *args, **kwargs):
            if os.fspath(path) == root:
                raise PermissionError("Root unavailable")
            return stat(path, *args, **kwargs)
        patch.setattr("reconciliation.os.stat", failed)
        assert library.run("rebuild_inventory")["status"] == "error"
    result = library.run("preview_cleanup")
    assert result["reconciliation"]["discovery_scanned_roots"] == 1
    assert result["reconciliation"]["catalogue_modes"] == ["metadata"]
