from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("aduana", "0004_detection_color"),
    ]

    operations = [
        migrations.AddField(
            model_name="containerdetection",
            name="roi_name",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
