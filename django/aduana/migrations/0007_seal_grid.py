from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("aduana", "0006_add_truck_id")]

    operations = [
        migrations.AddField(
            model_name="containerevent",
            name="seal_grid",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
