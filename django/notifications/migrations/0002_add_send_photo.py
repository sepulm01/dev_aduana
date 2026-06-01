from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationrule",
            name="send_photo",
            field=models.BooleanField(default=False),
        ),
    ]
