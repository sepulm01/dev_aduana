from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("devices", "0007_device_failure_count"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="deepstream_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
