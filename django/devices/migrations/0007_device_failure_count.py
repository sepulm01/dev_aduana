from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("devices", "0006_add_ivs_rule_device_event"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="failure_count",
            field=models.IntegerField(default=0),
        ),
    ]
