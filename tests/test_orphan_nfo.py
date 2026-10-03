"""Transition from unfiltered legacy/Emby NFOs to active metadata filters."""

import errno
import json
from pathlib import Path

import pytest

from tests.test_reconciliation import library, media, relation, owned, Snapshot, Query


def leftovers(library, kind='series', **metadata):
    obj = media(10, **{'year': 1934, **metadata})
    library.rows['series' if kind == 'series' else 'movies'].append(relation(obj, kind))
    root = library.settings['series_root_folder' if kind == 'series' else 'root_folder']
    method = library.p._series_target_folder if kind == 'series' else library.p._movie_target_paths
    folder = Path(method(obj, root)[0])
    folder.mkdir(parents=True)
    (folder / ('tvshow.nfo' if kind == 'series' else 'movie.nfo')).write_bytes(b'legacy or Emby metadata')
    (folder / 'Season 01').mkdir()
    (folder / 'Season 01' / 'episode.nfo').write_bytes(b'edited metadata\x00')
    return obj, folder


def archived(library):
    return library.tmp / 'state' / 'filtered-nfo'


@pytest.mark.parametrize('kind', ['movie', 'series'])
def test_untracked_nfo_only_output_archives_with_nfo_writing_off(library, kind):
    _, folder = leftovers(library, kind)
    before = {p.name: p.read_bytes() for p in folder.rglob('*.nfo')}
    result = library.run('generate_movies' if kind == 'movie' else 'generate_series',
                         **{f'{kind}_earliest_year': '2000'}, generate_nfo=False,
                         generate_series_nfo=False, deletion_scope='strm')
    assert result['status'] == 'ok'
    assert not folder.exists() and not library.calls
    assert {p.name: p.read_bytes() for p in archived(library).rglob('*.nfo')} == before
    report = result['reconciliation']
    assert report['filter_nfo_folders_archived'] == 1 and report['filter_nfo_archived'] == 2
    assert report['timings']['filter_nfo_metadata_read']['calls'] == 1
    events = [json.loads(line) for line in next(archived(library).rglob('manifest.jsonl')).read_text().splitlines()]
    assert [event['event'] for event in events] == ['planned', 'archived']
    assert events[1]['source'] == str(folder)
    again = library.run('generate_series' if kind == 'series' else 'generate_movies',
                        **{f'{kind}_earliest_year': '2000'})
    assert again['reconciliation']['filter_nfo_folders_archived'] == 0


def test_ownership_strm_only_cleanup_archives_nfos_without_needing_filters(library):
    show = media(10, year=1934)
    library.rows['series'].append(relation(show, 'series'))
    library.rows['episodes'].append(relation(media(11, series=show, season_number=1, episode_number=1), 'episode'))
    library.run('generate_series')
    root = Path(library.settings['series_root_folder'])
    nfos = {p.name: p.read_bytes() for p in root.rglob('*.nfo')}
    library.state['snapshot'] = Snapshot([owned(show, 'series')])
    removed = library.run('generate_series', media_library_enabled=True, deletion_scope='strm')
    assert not list(root.rglob('*.strm')) and not list(root.rglob('*.nfo'))
    assert removed['reconciliation']['ownership_nfo_folders_archived'] == 1
    result = library.run('generate_series', media_library_enabled=True,
                         series_earliest_year='2000', generate_series_nfo=False)
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0
    assert not list(root.rglob('*.nfo'))
    assert {p.name: p.read_bytes() for p in archived(library).rglob('*.nfo')} == nfos


def test_preview_is_read_only_and_reports_same_archive_as_generation(library):
    _, folder = leftovers(library)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*.nfo')}
    preview = library.run('preview_cleanup', series_earliest_year='2000')
    assert preview['reconciliation']['filter_nfo_folders_candidates'] == 1
    assert preview['reconciliation']['filter_nfo_candidates'] == 2
    assert not archived(library).exists()
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob('*.nfo')}
    result = library.run('generate_series', series_earliest_year='2000')
    assert result['reconciliation']['filter_nfo_archived'] == 2


@pytest.mark.parametrize('extra', ['unmanaged.strm', 'poster.jpg', 'notes.txt'])
def test_strms_and_unrelated_files_protect_entire_folder(library, extra):
    _, folder = leftovers(library)
    (folder / extra).write_text('preserve')
    result = library.run('generate_series', series_earliest_year='2000')
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0
    assert result['reconciliation']['filter_nfo_folders_preserved'] == 1
    assert (folder / extra).read_text() == 'preserve'
    assert not archived(library).exists()


def test_passing_source_protects_shared_nfo_only_path(library):
    _, folder = leftovers(library, rating='1')
    library.rows['series'].append(relation(media(10, year=1934, rating='9'), 'series'))
    result = library.run('generate_series', series_minimum_score='8')
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0 and folder.exists()


def test_inactive_filters_and_wrong_media_action_leave_nfos(library):
    _, folder = leftovers(library)
    assert library.run()['reconciliation']['filter_nfo_folders_archived'] == 0
    assert library.run(series_earliest_year='2000')['reconciliation']['filter_nfo_folders_archived'] == 0
    assert folder.exists() and not archived(library).exists()


def test_complete_native_path_lookup_failure_prevents_tracked_removal(library, monkeypatch):
    library.rows['movies'].append(relation(media(1, year=1914)))
    library.run()
    original = Query.values_list
    def fail(self, *fields):
        if 'category__name' in fields:
            raise OSError('native projection unavailable')
        return original(self, *fields)
    monkeypatch.setattr(Query, 'values_list', fail)
    result = library.run(movie_earliest_year='2000')
    assert result['status'] == 'error'
    assert list(Path(library.settings['root_folder']).rglob('*.strm'))


def test_cross_device_archive_verifies_copy_before_original_removal(library, monkeypatch):
    import orphan_nfo
    _, folder = leftovers(library)
    monkeypatch.setattr(orphan_nfo.os, 'rename', lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, 'cross-device')))
    result = library.run('generate_series', series_earliest_year='2000')
    assert result['reconciliation']['filter_nfo_archived'] == 2 and not folder.exists()
    assert len(list(archived(library).rglob('*.nfo'))) == 2


def test_cross_device_copy_failure_leaves_originals_retryable(library, monkeypatch):
    import orphan_nfo
    _, folder = leftovers(library)
    with monkeypatch.context() as patch:
        patch.setattr(orphan_nfo.os, 'rename', lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, 'cross-device')))
        patch.setattr(orphan_nfo.shutil, 'copytree', lambda *_a, **_k: (_ for _ in ()).throw(OSError('disk full')))
        result = library.run('generate_series', series_earliest_year='2000')
    assert result['reconciliation']['filter_nfo_errors'] == 1
    assert len(list(folder.rglob('*.nfo'))) == 2
    assert library.run('generate_series', series_earliest_year='2000')['reconciliation']['filter_nfo_archived'] == 2


def test_cross_device_changed_metadata_is_not_removed(library, monkeypatch):
    import orphan_nfo
    _, folder = leftovers(library)
    original = orphan_nfo.shutil.copytree
    def copying(src, dst, *args, **kwargs):
        original(src, dst, *args, **kwargs)
        if Path(src) == folder:
            (src / 'tvshow.nfo').write_bytes(b'changed during copy')
    monkeypatch.setattr(orphan_nfo.os, 'rename', lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, 'cross-device')))
    monkeypatch.setattr(orphan_nfo.shutil, 'copytree', copying)
    result = library.run('generate_series', series_earliest_year='2000')
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0
    assert (folder / 'tvshow.nfo').read_bytes() == b'changed during copy'
    assert len(list(folder.rglob('*.nfo'))) == 2


def test_preview_accounts_for_strm_removal_before_archiving_edited_nfo(library):
    library.rows['movies'].append(relation(media(1, year=1914)))
    library.run()
    root = Path(library.settings['root_folder'])
    nfo = next(root.rglob('*.nfo'))
    nfo.write_text('edited metadata')
    preview = library.run('preview_cleanup', movie_earliest_year='2000')
    assert preview['reconciliation']['filter_nfo_folders_candidates'] == 1
    assert preview['reconciliation']['filter_nfo_candidates'] == 1
    assert nfo.exists() and list(root.rglob('*.strm'))
    result = library.run(movie_earliest_year='2000')
    assert result['reconciliation']['filter_nfo_archived'] == 1


def test_nfo_year_is_never_a_filter_source(library):
    _, folder = leftovers(library, year=2024)
    (folder / 'tvshow.nfo').write_text('<tvshow><year>1934</year></tvshow>')
    result = library.run('generate_series', series_earliest_year='2020')
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0
    assert (folder / 'tvshow.nfo').exists()


def test_archive_location_inside_output_is_rejected(library):
    _, folder = leftovers(library)
    result = library.run('generate_series', series_earliest_year='2000', root_folder=str(library.tmp))
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0 and folder.exists()


def test_symlink_inside_folder_is_preserved(library, tmp_path):
    _, folder = leftovers(library)
    external = tmp_path / 'external.nfo'
    external.write_text('outside metadata')
    try:
        (folder / 'link.nfo').symlink_to(external)
    except OSError:
        pytest.skip('symlinks unavailable on this platform')
    result = library.run('generate_series', series_earliest_year='2000')
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0
    assert external.read_text() == 'outside metadata' and folder.exists()


def test_native_path_generation_includes_category_and_tmdb_format(library):
    obj = media(10, year=1934)
    library.rows['series'].append(relation(obj, 'series'))
    folder = Path(library.p._series_target_folder(obj, library.settings['series_root_folder'],
                                                 'Action', True, True, 'jellyfin')[0])
    folder.mkdir(parents=True)
    (folder / 'tvshow.nfo').write_text('metadata')
    result = library.run('generate_series', series_earliest_year='2000',
                         nest_series_by_category=True, append_tmdb_id_to_folder=True,
                         tmdb_tag_format='jellyfin')
    assert result['reconciliation']['filter_nfo_folders_archived'] == 1 and not folder.exists()


def test_passing_other_media_type_protects_shared_output_root(library):
    _, folder = leftovers(library, 'movie')
    library.rows['series'].append(relation(media(10, year=1934), 'series'))
    result = library.run(movie_earliest_year='2000',
                         series_root_folder=library.settings['root_folder'])
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0 and folder.exists()


def test_unresolved_old_layout_is_preserved(library):
    _, folder = leftovers(library)
    previous = folder.with_name('historical-naming-layout')
    folder.rename(previous)
    result = library.run('generate_series', series_earliest_year='2000')
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0
    assert previous.exists() and not archived(library).exists()


def test_nfo_settings_section_matches_manifest_and_preserves_defaults():
    from plugin import Plugin
    data = json.loads(Path('plugin.json').read_text(encoding='utf8'))
    assert data['fields'] == Plugin.fields
    section = next(field for field in Plugin.fields if field['id'] == '_section_nfo')
    assert section['type'] == 'info' and section['label'] and section['description']
    position = Plugin.fields.index(section)
    assert [field['id'] for field in Plugin.fields[position + 1:position + 4]] == [
        'generate_nfo', 'generate_series_nfo', 'nfo_omit_title']
    assert all(next(field for field in Plugin.fields if field['id'] == key)['default']
               for key in ['generate_nfo', 'generate_series_nfo'])


@pytest.mark.parametrize('kind', ['movie', 'series'])
def test_owned_legacy_nfo_only_folder_archives_without_active_filters(library, kind):
    obj, folder = leftovers(library, kind, year=2026)
    library.state['snapshot'] = Snapshot([owned(obj, kind)])
    result = library.run('generate_series' if kind == 'series' else 'generate_movies',
                         media_library_enabled=True, generate_nfo=False, generate_series_nfo=False)
    assert result['reconciliation']['ownership_nfo_folders_archived'] == 1
    assert result['reconciliation']['ownership_nfo_archived'] == 2
    assert result['reconciliation']['filter_nfo_folders_archived'] == 0
    assert not folder.exists() and not library.calls


def test_owned_orphan_episode_nfos_without_tvshow_nfo_archives(library):
    obj, folder = leftovers(library, year=2026)
    (folder / 'tvshow.nfo').unlink()
    library.state['snapshot'] = Snapshot([owned(obj, 'series', [(1, 1), (1, 2)])])
    result = library.run('generate_series', media_library_enabled=True)
    assert result['reconciliation']['ownership_nfo_archived'] == 1
    assert not folder.exists() and len(list(archived(library).rglob('*.nfo'))) == 1


@pytest.mark.parametrize('scope', ['strm', 'strm_nfo'])
def test_owned_preview_matches_archives_after_planned_strm_deletion(library, scope):
    library.rows['movies'].append(relation(media(10)))
    library.run()
    root = Path(library.settings['root_folder'])
    nfo = next(root.rglob('*.nfo'));nfo.write_text('Emby-edited metadata')
    library.state['snapshot'] = Snapshot([owned(media(10))])
    preview = library.run('preview_cleanup', media_library_enabled=True, deletion_scope=scope)
    assert preview['reconciliation']['ownership_nfo_folders_candidates'] == 1
    assert preview['reconciliation']['ownership_nfo_candidates'] == 1 and nfo.exists()
    result = library.run(media_library_enabled=True, deletion_scope=scope)
    assert result['reconciliation']['ownership_nfo_archived'] == 1 and not nfo.exists()
    assert next(archived(library).rglob(nfo.name)).read_text() == 'Emby-edited metadata'


def test_filter_and_ownership_preview_never_double_count_same_folder(library):
    obj, _ = leftovers(library)
    library.state['snapshot'] = Snapshot([owned(obj, 'series')])
    preview = library.run('preview_cleanup', media_library_enabled=True, series_earliest_year='2020')
    assert preview['reconciliation']['filter_nfo_folders_candidates'] == 1
    assert preview['reconciliation']['ownership_nfo_folders_candidates'] == 0


def test_episode_mode_preserves_nfo_only_show_for_missing_episodes(library):
    obj, folder = leftovers(library, year=2026)
    library.state['snapshot'] = Snapshot([owned(obj, 'series', [(1, 1)])])
    result = library.run('generate_series', media_library_enabled=True, media_tv_mode='episodes')
    assert result['reconciliation']['ownership_nfo_folders_archived'] == 0 and folder.exists()


@pytest.mark.parametrize('settings', [{'media_library_enabled': False},
    {'media_library_enabled': True, 'media_duplicate_cleanup': 'manual'},
    {'media_library_enabled': True, 'media_duplicate_cleanup': 'rescan'}])
def test_ownership_archiving_obeys_enabled_integration_and_cleanup_timing(library, settings):
    obj, folder = leftovers(library, year=2026)
    library.state['snapshot'] = Snapshot([owned(obj, 'series')])
    result = library.run('generate_series', **settings)
    assert result['reconciliation']['ownership_nfo_folders_archived'] == 0 and folder.exists()


def test_selective_cleanup_explicitly_applies_manual_ownership_archival(library):
    obj, folder = leftovers(library, year=2026)
    library.state['snapshot'] = Snapshot([owned(obj, 'series')])
    result = library.run('selective_cleanup', media_library_enabled=True, media_duplicate_cleanup='manual')
    assert result['reconciliation']['ownership_nfo_folders_archived'] == 1 and not folder.exists()


def test_failed_emby_snapshot_does_not_archive_ownership_metadata(library):
    _, folder = leftovers(library, year=2026)
    library.state['error'] = OSError('Emby offline')
    result = library.run('generate_series', media_library_enabled=True)
    assert result['reconciliation']['ownership_nfo_folders_archived'] == 0 and folder.exists()


def test_conflicting_provider_ids_protect_nfo_folder(library):
    from media_library import Identity, OwnedMedia
    obj, folder = leftovers(library, year=2026, imdb_id='native-imdb')
    library.state['snapshot'] = Snapshot([OwnedMedia(Identity('series', obj.name, obj.year,
                                                       obj.tmdb_id, 'conflicting-imdb'))])
    result = library.run('generate_series', media_library_enabled=True)
    assert result['reconciliation']['ownership_nfo_folders_archived'] == 0 and folder.exists()


def test_owned_orphan_scope_matches_existing_cleanup_in_both_roots(library):
    obj, folder = leftovers(library, year=2026)
    library.state['snapshot'] = Snapshot([owned(obj, 'series')])
    result = library.run('generate_movies', media_library_enabled=True)
    assert result['reconciliation']['ownership_nfo_folders_archived'] == 1 and not folder.exists()


def test_failed_native_ownership_lookup_preserves_tracked_strms(library, monkeypatch):
    library.rows['movies'].append(relation(media(10)))
    library.run()
    library.state['snapshot'] = Snapshot([owned(media(10))])
    original = Query.values_list
    def fail(self, *fields):
        if 'category__name' in fields:
            raise OSError('native path lookup failed')
        return original(self, *fields)
    monkeypatch.setattr(Query, 'values_list', fail)
    result = library.run(media_library_enabled=True)
    assert result['reconciliation']['ownership_nfo_folders_archived'] == 0
    assert list(Path(library.settings['root_folder']).rglob('*.strm'))
    assert any('server cleanup disabled' in warning for warning in result['reconciliation']['warnings'])
