from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("devices", "0009_analyticspreset"),
    ]

    operations = [
        migrations.AddField(
            model_name="analyticspreset",
            name="snapshot",
            field=models.TextField(blank=True, default=""),
        ),
    ]
