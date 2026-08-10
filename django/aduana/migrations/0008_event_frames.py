from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("aduana", "0007_seal_grid")]

    operations = [
        migrations.AddField(
            model_name="containerevent",
            name="frame_src0",
            field=models.ImageField(blank=True, null=True, upload_to="frames/"),
        ),
        migrations.AddField(
            model_name="containerevent",
            name="frame_src1",
            field=models.ImageField(blank=True, null=True, upload_to="frames/"),
        ),
    ]
