import copy
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tests.test_reconciliation import library, media, relation


def forbid_provider(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('Plugin must use stored Dispatcharr data only')

    monkeypatch.setitem(sys.modules, 'core.xtream_codes', NS(Client=forbidden))
    monkeypatch.setitem(sys.modules, 'apps.vod.tasks', NS(
        refresh_series_episodes=forbidden, batch_process_episodes=forbidden))
    return attempts


@pytest.mark.parametrize('action', [
    'generate_movies', 'generate_series', 'rescan_all', 'preview_cleanup',
    'selective_cleanup', 'rebuild_inventory',
])
def test_actions_never_fetch_providers_or_mutate_native_freshness(library, monkeypatch, action):
    attempts = forbid_provider(monkeypatch)
    show = media(10)
    show_relation = relation(show, 'series')
    show_relation.custom_properties = {'episodes_fetched': False, 'detailed_fetched': False}
    show_relation.last_episode_refresh = 'original series timestamp'
    episode = relation(media(1, series=show, season_number=1, episode_number=1), 'episode')
    episode.last_seen = 'original episode timestamp'
    properties = copy.deepcopy(show_relation.custom_properties)
    library.rows['movies'].append(relation(media(2)))
    library.rows['series'].append(show_relation)
    library.rows['episodes'].append(episode)
    result = library.run(action, refresh_existing=True, m3u_cleanup_enabled=True,
                         m3u_cleanup_timing='rescan')
    assert result['status'] == 'ok' and result.get('errors', 0) == 0
    assert not attempts and not library.calls
    assert show_relation.custom_properties == properties
    assert show_relation.last_episode_refresh == 'original series timestamp'
    assert episode.last_seen == 'original episode timestamp'
    assert not any(key.startswith('provider_') for key in result['reconciliation']['timings'])


def test_missing_database_episodes_do_not_trigger_provider_fallback(library, monkeypatch):
    attempts = forbid_provider(monkeypatch)
    show = relation(media(10), 'series')
    show.custom_properties = {}
    library.rows['series'].append(show)
    result = library.run('generate_series', refresh_existing=True)
    assert result['status'] == 'ok' and result['episodes_created'] == 0
    assert not attempts and not list(Path(library.settings['series_root_folder']).rglob('*.strm'))


def test_database_episode_additions_and_stream_changes_are_applied_without_fetching(library, monkeypatch):
    attempts = forbid_provider(monkeypatch)
    show = media(10)
    library.rows['series'].append(relation(show, 'series'))
    first = relation(media(1, series=show, season_number=1, episode_number=1), 'episode')
    library.rows['episodes'].append(first)
    assert library.run('generate_series', refresh_existing=True)['episodes_created'] == 1
    assert library.run('generate_series', refresh_existing=True)['episodes_created'] == 0
    first.stream_id = 'changed database stream'
    result = library.run('generate_series', refresh_existing=True)
    assert result['episodes_refreshed'] == 1
    library.rows['episodes'].append(relation(
        media(2, series=show, season_number=1, episode_number=2), 'episode'))
    assert library.run('generate_series', refresh_existing=True)['episodes_created'] == 1
    assert not attempts
