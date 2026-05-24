import json

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from devices.models import Device

PATH_FIELDS = ["host", "port", "username", "password", "stream_uris"]


def _stable(instance, field):
    val = getattr(instance, field)
    return json.dumps(val, sort_keys=True) if isinstance(val, dict) else val


@receiver(pre_save, sender=Device)
def device_pre_save(sender, instance, **kwargs):
    if not instance.pk:
        instance._pre_save_values = {}
        return
    try:
        old = Device.objects.get(pk=instance.pk)
        instance._pre_save_values = {
            field: _stable(old, field) for field in PATH_FIELDS
        }
    except Device.DoesNotExist:
        instance._pre_save_values = {}


@receiver(post_save, sender=Device)
def device_post_save(sender, instance, created, **kwargs):
    if getattr(instance, "_skip_stream_refresh", False):
        return
    pre = getattr(instance, "_pre_save_values", None)
    if pre is None:
        return
    changed = any(
        pre.get(field) != _stable(instance, field) for field in PATH_FIELDS
    )
    if changed:
        from devices.tasks import refresh_device_streams

        refresh_device_streams.delay(instance.id)
