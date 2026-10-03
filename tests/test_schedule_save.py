"""Exercise the actual Django Save signals and beat records in an isolated database."""
import os
from pathlib import Path
import subprocess
import sys


def test_save_drives_schedule_in_real_database(tmp_path):
    script = r'''
import os,sys,json,types,shutil,importlib
from pathlib import Path
from django.conf import settings
settings.configure(SECRET_KEY='test-only',INSTALLED_APPS=['django_celery_beat'],USE_TZ=True,
 DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':sys.argv[1]}})
import django
django.setup()
from django.core.management import call_command
call_command('migrate',verbosity=0)
from django.db import models,connection,transaction
class PluginConfig(models.Model):
 key=models.CharField(max_length=100,unique=True)
 settings=models.JSONField(default=dict)
 enabled=models.BooleanField(default=True)
 updated_at=models.DateTimeField(auto_now=True)
 class Meta:app_label='plugin_test'
with connection.schema_editor() as editor:editor.create_model(PluginConfig)
sys.modules['apps.plugins.models']=types.SimpleNamespace(PluginConfig=PluginConfig)
import plugin
from schedule_settings import install
from django_celery_beat.models import CrontabSchedule,PeriodicTask
cron=CrontabSchedule.objects.create(minute='0',hour='3',day_of_month='*',month_of_year='*',day_of_week='*',timezone='UTC')
PeriodicTask.objects.create(name=plugin.Plugin.SCHEDULE_TASK_NAME,task=plugin.Plugin.SCHEDULED_TASK_CELERY_NAME,
 crontab=cron,enabled=True,kwargs=json.dumps({'settings':{'media_server_token':'obsolete'}}))
cfg=PluginConfig.objects.create(key='vod2mlib',settings={'root_folder':'/actual/Movies'})
# Import through the installed package path to exercise automatic hook registration.
root=Path(sys.argv[1]).parent/'plugins';target=root/'vod2mlib';target.mkdir(parents=True)
for source in Path.cwd().glob('*.py'):shutil.copy2(source,target/source.name)
os.environ['DISPATCHARR_PLUGINS_DIR']=str(root)
pkg=types.ModuleType('plugins');pkg.__path__=[str(root)];sys.modules['plugins']=pkg
plugin=importlib.import_module('plugins.vod2mlib.plugin')
cfg.refresh_from_db();assert cfg.settings['schedule_enabled'] is True
assert cfg.settings['root_folder']=='/actual/Movies'
def task():return PeriodicTask.objects.get(name=plugin.Plugin.SCHEDULE_TASK_NAME)
assert task().kwargs=='{}' and task().queue=='dvr'
cfg.settings.update(schedule_cron='15 4 * * 1-5',schedule_timezone='America/New_York',schedule_target='generate_series')
cfg.save(update_fields=['settings','updated_at'])
t=task();assert t.enabled and t.crontab.minute=='15' and t.crontab.hour=='4'
assert str(t.crontab.timezone)=='America/New_York' and t.kwargs=='{}'
old=dict(cfg.settings)
for field,value in [('schedule_cron','61 4 * * *'),('schedule_cron','0 24 * * *'),('schedule_cron','bad'),('schedule_timezone','Invalid/Zone')]:
 cfg.settings={**old,field:value}
 try:cfg.save(update_fields=['settings','updated_at'])
 except ValueError:pass
 else:raise AssertionError('Invalid cron/timezone saved')
 cfg.refresh_from_db();assert cfg.settings==old
assert task().enabled
cfg.settings={**old,'schedule_enabled':False,'schedule_cron':'invalid while disabled'}
cfg.save(update_fields=['settings','updated_at']);assert not task().enabled
assert plugin.Plugin._scheduled_settings()['root_folder']=='/actual/Movies'
assert plugin.Plugin._scheduled_settings(require_enabled=True) is None
plugin.action_runner.run_and_wait=lambda *args: (_ for _ in ()).throw(AssertionError('Disabled tick ran'))
assert plugin._vod2mlib_scheduled_rescan.run(action='generate_movies',settings={'schedule_enabled':True})['status']=='ok'
cfg.settings=old;cfg.save(update_fields=['settings','updated_at']);assert task().enabled
cfg.enabled=False;cfg.save(update_fields=['enabled','updated_at']);assert not task().enabled
cfg.enabled=True;cfg.save(update_fields=['enabled','updated_at']);assert task().enabled
# Commit callbacks must not apply rolled-back edits.
try:
 with transaction.atomic():
  cfg.settings={**old,'schedule_cron':'0 8 * * *'}
  cfg.save(update_fields=['settings','updated_at'])
  raise RuntimeError('rollback')
except RuntimeError:pass
assert task().crontab.hour=='4'
cfg.refresh_from_db();assert cfg.settings==old
install(plugin.Plugin);assert PeriodicTask.objects.filter(name=plugin.Plugin.SCHEDULE_TASK_NAME).count()==1
cfg.delete();assert not PeriodicTask.objects.filter(name=plugin.Plugin.SCHEDULE_TASK_NAME).exists()
# A new installation never creates a schedule until explicitly enabled.
cfg=PluginConfig.objects.create(key='vod2mlib',settings={})
install(plugin.Plugin);cfg.refresh_from_db();assert cfg.settings['schedule_enabled'] is False
assert not PeriodicTask.objects.filter(name=plugin.Plugin.SCHEDULE_TASK_NAME).exists()
cfg.settings.update(schedule_enabled=True,schedule_cron='0 6 * * *');cfg.save(update_fields=['settings','updated_at'])
assert task().enabled and task().crontab.hour=='6'
print('Save, validation, toggle, legacy upgrade, commit, disable, and uninstall verified')
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'schedule.sqlite3')],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                            timeout=90, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
    assert result.returncode == 0, result.stdout + result.stderr
