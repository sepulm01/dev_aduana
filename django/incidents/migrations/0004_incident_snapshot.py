from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("incidents", "0003_delete_escalationlevel"),
    ]

    operations = [
        migrations.AddField(
            model_name="incident",
            name="snapshot",
            field=models.ImageField(
                blank=True, null=True, upload_to="incidents/snapshots/%Y/%m/"
            ),
        ),
    ]
