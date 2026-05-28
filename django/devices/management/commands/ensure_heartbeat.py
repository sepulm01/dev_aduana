from django.core.management.base import BaseCommand
from django_celery_beat.models import IntervalSchedule, PeriodicTask


class Command(BaseCommand):
    help = "Ensure the camera orchestrator periodic task exists in the database"

    def handle(self, **options):
        schedule, _ = IntervalSchedule.objects.get_or_create(
            every=5,
            period=IntervalSchedule.SECONDS,
        )
        task, created = PeriodicTask.objects.update_or_create(
            name="orchestrate-cameras-every-5s",
            defaults={
                "interval": schedule,
                "task": "devices.tasks.orchestrate_cameras",
                "enabled": True,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Created orchestrator (every 5s)"))
        else:
            self.stdout.write("Orchestrator already exists")

        PeriodicTask.objects.filter(name="heartbeat-deepstream-streams").delete()
        PeriodicTask.objects.filter(name="deepstream-heartbeat-every-30s").delete()
