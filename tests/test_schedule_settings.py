"""Scheduled jobs resolve the same saved settings and defaults as manual actions."""
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from plugin import Plugin


@pytest.fixture
def saved(monkeypatch):
    values = {'root_folder': '/saved/Movies', 'series_root_folder': '/saved/Series',
              'media_server_token': 'new-token', 'series_workers': '6',
              'batch_size': '100', 'action_timeout_minutes': 90,
              'schedule_target': 'generate_movies', 'schedule_enabled': True}
    monkeypatch.setitem(sys.modules, 'apps.plugins.models', NS(
        PluginConfig=NS(objects=NS(get=lambda **kw: NS(settings=values, enabled=True)))))
    return values


def test_all_settings_and_defaults_match_manual_context(saved):
    expected = dict(saved)
    for field in Plugin.fields:
        if 'default' in field:
            expected.setdefault(field['id'], field['default'])
    assert Plugin._scheduled_settings({'root_folder': '/old', 'media_server_token': 'old-token'}) == expected
    assert saved == {key: expected[key] for key in saved}


def test_next_run_uses_changed_and_cleared_settings(saved):
    first = Plugin._scheduled_settings()
    saved.update(root_folder='/changed', media_server_token='', series_workers='2',
                 movie_earliest_year='2010', schedule_target='generate_series')
    second = Plugin._scheduled_settings()
    assert first['root_folder'] == '/saved/Movies'
    assert first['media_server_token'] == 'new-token'
    assert second['root_folder'] == '/changed' and second['media_server_token'] == ''
    assert second['series_workers'] == '2' and second['movie_earliest_year'] == '2010'
    assert second['schedule_target'] == 'generate_series'


def test_missing_saved_config_does_not_fall_back_to_legacy(monkeypatch):
    def missing(**kw):
        raise LookupError('Plugin config missing')
    monkeypatch.setitem(sys.modules, 'apps.plugins.models', NS(PluginConfig=NS(objects=NS(get=missing))))
    with pytest.raises(LookupError):
        Plugin._scheduled_settings({'root_folder': '/old'})


@pytest.mark.parametrize('key,value', [('series_workers', '7'), ('movie_earliest_year', 'invalid')])
def test_invalid_saved_settings_fail_before_action(saved, key, value):
    saved[key] = value
    with pytest.raises(ValueError):
        Plugin._scheduled_settings()


def test_test_fire_enqueues_no_settings_or_target(saved, monkeypatch):
    task = NS(kwargs='invalid legacy kwargs', enabled=True)
    monkeypatch.setitem(sys.modules, 'django_celery_beat.models', NS(
        PeriodicTask=NS(objects=NS(filter=lambda **kw: NS(first=lambda: task)))))
    calls = []
    monkeypatch.setitem(sys.modules, 'celery', NS(current_app=NS(
        send_task=lambda *a, **kw: calls.append(kw) or NS(id='test'))))
    result = Plugin()._schedule_test_fire({}, logging.getLogger('test'))
    assert result['status'] == 'ok' and result['fired_action'] == 'generate_movies'
    assert calls == [{'kwargs': {}, 'queue': 'dvr'}]


def test_worker_reads_settings_at_execution_and_ignores_legacy_target(saved, monkeypatch):
    monkeypatch.setitem(sys.modules, 'celery', NS(shared_task=lambda **kw: lambda fn: fn))
    spec = importlib.util.spec_from_file_location('_schedule_test_plugin', Path('plugin.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module.action_runner, 'run_and_wait',
                        lambda *args: calls.append(args) or {'status': 'ok'})
    monkeypatch.setitem(sys.modules, 'django.utils', NS(timezone=NS(now=lambda: 'now')))
    monkeypatch.setitem(sys.modules, 'django_celery_beat.models', NS(PeriodicTask=NS(
        objects=NS(filter=lambda **kw: NS(update=lambda **kw: None)))))
    saved.update(schedule_target='generate_series', series_root_folder='/changed/Series')
    assert module._vod2mlib_scheduled_rescan(action='generate_movies', settings={'series_root_folder': '/old'}) == {'status': 'ok'}
    assert calls[0][0] == 'generate_series' and calls[0][2]['series_root_folder'] == '/changed/Series'
    saved['schedule_target'] = 'remove_schedule'
    with pytest.raises(ValueError):
        module._vod2mlib_scheduled_rescan()
    assert len(calls) == 1


def test_status_does_not_print_legacy_credentials(saved, monkeypatch, caplog):
    task = NS(name=Plugin.SCHEDULE_TASK_NAME, crontab=NS(minute='0', hour='3',
              day_of_month='*', month_of_year='*', day_of_week='*', timezone='UTC'),
              kwargs=json.dumps({'settings': {'media_server_token': 'old-secret'}}),
              task=Plugin.SCHEDULED_TASK_CELERY_NAME, last_run_at=None, enabled=True, total_run_count=0)
    monkeypatch.setitem(sys.modules, 'django_celery_beat.models', NS(PeriodicTask=NS(
        objects=NS(filter=lambda **kw: NS(first=lambda: task)))))
    with caplog.at_level(logging.INFO):
        result = Plugin()._schedule_status(saved, logging.getLogger('test'))
    assert result['settings_source'] == 'current_saved'
    assert result['target'] == 'generate_movies'
    assert 'old-secret' not in caplog.text and 'new-token' not in caplog.text
