from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aduana", "0004_detection_color"),
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
                ("preset_token", models.CharField(max_length=120)),
                ("preset_name", models.CharField(blank=True, default="", max_length=120)),
                ("shapes", models.JSONField(blank=True, default=list)),
                ("snapshot", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="analytics_presets",
                        to="devices.device",
                    ),
                ),
            ],
            options={
                "unique_together": {("device", "preset_token")},
            },
        ),
    ]
