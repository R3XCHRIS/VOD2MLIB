"""Synchronize the beat trigger with saved plugin settings, without native patches."""
import logging

KEY = 'vod2mlib'
LOG = logging.getLogger('vod2mlib.schedule')


def install(plugin_class):
    from django.db import transaction
    from django.db.models.signals import pre_save, post_save, post_delete
    from apps.plugins.models import PluginConfig
    from django_celery_beat.models import PeriodicTask

    def relevant(instance, raw=False, update_fields=None, **kwargs):
        return (not raw and instance.key == KEY and
                (update_fields is None or bool({'settings', 'enabled'} & set(update_fields))))

    def validate(sender, instance, **kwargs):
        if relevant(instance, **kwargs):
            settings = dict(instance.settings or {})
            plugin_class()._validate_saved_settings(settings)

    def synchronize(sender, instance, **kwargs):
        if relevant(instance, **kwargs):
            def committed():
                with transaction.atomic():
                    cfg = PluginConfig.objects.select_for_update().filter(pk=instance.pk).first()
                    if cfg is not None:
                        plugin_class()._sync_schedule(cfg.settings or {}, LOG, plugin_enabled=cfg.enabled)
            transaction.on_commit(committed)

    def remove(sender, instance, **kwargs):
        if instance.key == KEY:
            PeriodicTask.objects.filter(name=plugin_class.SCHEDULE_TASK_NAME).delete()

    for signal, callback, name in ((pre_save, validate, 'validate'),
                                    (post_save, synchronize, 'synchronize'),
                                    (post_delete, remove, 'remove')):
        uid = KEY + '.schedule.' + name
        signal.disconnect(sender=PluginConfig, dispatch_uid=uid)
        signal.connect(callback, sender=PluginConfig, dispatch_uid=uid, weak=False)

    with transaction.atomic():
        cfg = PluginConfig.objects.select_for_update().filter(key=KEY).first()
        if cfg is not None:
            # Preserve existing schedule state once; fresh installations default off.
            if 'schedule_enabled' not in (cfg.settings or {}):
                existing = PeriodicTask.objects.filter(name=plugin_class.SCHEDULE_TASK_NAME).first()
                cfg.settings = {**(cfg.settings or {}), 'schedule_enabled': bool(existing and existing.enabled)}
                cfg.save(update_fields=['settings', 'updated_at'])
            else:
                plugin_class()._sync_schedule(cfg.settings or {}, LOG, plugin_enabled=cfg.enabled)
