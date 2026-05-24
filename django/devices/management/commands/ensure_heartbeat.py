from django.core.management.base import BaseCommand
from django_celery_beat.models import IntervalSchedule, PeriodicTask


class Command(BaseCommand):
    help = "Ensure DeepStream heartbeat periodic task exists in the database"

    def handle(self, **options):
        schedule, _ = IntervalSchedule.objects.get_or_create(
            every=60,
            period=IntervalSchedule.SECONDS,
        )
        task, created = PeriodicTask.objects.update_or_create(
            name="heartbeat-deepstream-streams",
            defaults={
                "interval": schedule,
                "task": "devices.tasks.heartbeat_deepstream_streams",
                "enabled": True,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Created DeepStream heartbeat (every 60s)"))
        else:
            self.stdout.write("DeepStream heartbeat already exists")
