from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("incidents", "0002_initial"),
    ]

    operations = [
        migrations.DeleteModel(
            name="EscalationLevel",
        ),
    ]
