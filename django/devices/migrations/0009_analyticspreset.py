from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("devices", "0008_device_deepstream_enabled"),
    ]

    operations = [
        migrations.CreateModel(
            name="AnalyticsPreset",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="analytics_presets",
                        to="devices.device",
                    ),
                ),
                ("preset_token", models.CharField(max_length=120)),
                (
                    "preset_name",
                    models.CharField(blank=True, default="", max_length=120),
                ),
                ("shapes", models.JSONField(blank=True, default=list)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["device", "preset_token"],
                "unique_together": {("device", "preset_token")},
            },
        ),
    ]
