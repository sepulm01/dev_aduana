from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0016_device_snmp_community_device_snmp_enabled_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="Patrol",
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
                ("name", models.CharField(max_length=120)),
                ("is_active", models.BooleanField(default=True)),
                ("valid_from", models.DateTimeField(blank=True, null=True)),
                ("valid_until", models.DateTimeField(blank=True, null=True)),
                ("schedule", models.JSONField(blank=True, default=dict)),
                ("dwell_seconds", models.IntegerField(default=10)),
                ("speed", models.FloatField(default=1.0)),
                ("preset_order", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        related_name="patrols",
                        to="devices.device",
                    ),
                ),
            ],
            options={
                "ordering": ["device", "name"],
            },
        ),
    ]
