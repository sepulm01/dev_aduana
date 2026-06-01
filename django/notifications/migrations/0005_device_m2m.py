from django.db import migrations, models


def migrate_device_to_m2m(apps, schema_editor):
    NotificationRule = apps.get_model("notifications", "NotificationRule")
    for rule in NotificationRule.objects.exclude(device__isnull=True):
        rule.devices.add(rule.device)


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0004_add_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationrule",
            name="devices",
            field=models.ManyToManyField(
                blank=True,
                related_name="notification_rules",
                to="devices.Device",
            ),
        ),
        migrations.RunPython(migrate_device_to_m2m, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="notificationrule",
            name="device",
        ),
    ]
