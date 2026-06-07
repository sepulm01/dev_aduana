from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0013_deepstream_pipeline"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="source_type",
            field=models.CharField(
                choices=[("rtsp", "RTSP Camera"), ("file", "File (MP4)")],
                default="rtsp",
                max_length=10,
            ),
        ),
    ]
