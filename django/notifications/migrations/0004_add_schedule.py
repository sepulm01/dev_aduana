from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0003_min_duration_seconds"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationrule",
            name="schedule",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="notificationrule",
            name="valid_from",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="notificationrule",
            name="valid_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
