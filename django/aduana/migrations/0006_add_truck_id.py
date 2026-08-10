from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("aduana", "0005_detection_roi_name")]
    operations = [
        migrations.AddField(
            model_name="containerdetection",
            name="truck_id",
            field=models.BigIntegerField(default=0),
        ),
    ]
