from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0002_add_send_photo"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationrule",
            name="min_duration_seconds",
            field=models.IntegerField(default=0),
        ),
    ]
