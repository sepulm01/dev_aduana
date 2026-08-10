from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("aduana", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="containerdetection",
            name="frame_num",
            field=models.BigIntegerField(default=0),
        ),
    ]
