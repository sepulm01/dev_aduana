from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("devices", "0010_analyticspreset_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="analyticspreset",
            name="ptz_position",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
