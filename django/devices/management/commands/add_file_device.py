from django.core.management.base import BaseCommand, CommandError

from devices.models import Device


class Command(BaseCommand):
    help = "Creates a file-type Device (MP4) for testing"

    def add_arguments(self, parser):
        parser.add_argument("name", type=str, help="Display name for the dummy device")
        parser.add_argument("file_path", type=str, help="Container path, e.g. /opt/videos/test.mp4")
        parser.add_argument(
            "--pipeline",
            type=str,
            default="aduana",
            choices=["aduana"],
            help="DeepStream pipeline (default: aduana)",
        )

    def handle(self, *args, **options):
        name = options["name"]
        file_path = options["file_path"]
        pipeline = options["pipeline"]

        uri = f"file://{file_path}"

        device, created = Device.objects.get_or_create(
            host="file",
            port=0,
        )
        device.name = name
        device.username = "dummy"
        device.password = "dummy"
        device.source_type = "file"
        device.is_online = True
        device.deepstream_pipeline = pipeline
        device.stream_uris = {"main": uri}
        device.default_profile_token = "main"
        device.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Device {'created' if created else 'updated'}: {device.name} (id={device.id}, uri={uri})"
            )
        )
