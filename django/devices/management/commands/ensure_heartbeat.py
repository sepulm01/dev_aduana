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

        schedule_30, _ = IntervalSchedule.objects.get_or_create(
            every=30,
            period=IntervalSchedule.SECONDS,
        )
        for task_name, task_path in [
            ("monitoring-system-every-30s", "monitoring.tasks.collect_system"),
            ("monitoring-mediamtx-every-30s", "monitoring.tasks.collect_mediamtx"),
            ("monitoring-deepstream-every-30s", "monitoring.tasks.collect_deepstream"),
        ]:
            PeriodicTask.objects.update_or_create(
                name=task_name,
                defaults={
                    "interval": schedule_30,
                    "task": task_path,
                    "enabled": True,
                },
            )

        schedule_60, _ = IntervalSchedule.objects.get_or_create(
            every=60,
            period=IntervalSchedule.SECONDS,
        )
        PeriodicTask.objects.update_or_create(
            name="monitoring-snmp-every-60s",
            defaults={
                "interval": schedule_60,
                "task": "monitoring.tasks.collect_snmp",
                "enabled": True,
            },
        )

        schedule_10, _ = IntervalSchedule.objects.get_or_create(
            every=10,
            period=IntervalSchedule.SECONDS,
        )
        PeriodicTask.objects.update_or_create(
            name="patrol-controller-every-10s",
            defaults={
                "interval": schedule_10,
                "task": "devices.tasks.patrol_controller",
                "enabled": True,
            },
        )

        PeriodicTask.objects.filter(name="heartbeat-deepstream-streams").delete()
        PeriodicTask.objects.filter(name="deepstream-heartbeat-every-30s").delete()
