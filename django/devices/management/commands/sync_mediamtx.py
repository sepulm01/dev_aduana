from django.core.management.base import BaseCommand
from devices.models import Device
from onvif_utils.client import OnvifClient
from onvif_utils.media import MediaService
from onvif_utils.mediamtx_api import MediaMTXAPI


class Command(BaseCommand):
    help = "Recrea en MediaMTX los paths de todas las cámaras con credenciales"

    def add_arguments(self, parser):
        parser.add_argument(
            "--device-id",
            type=int,
            help="Sincronizar solo un dispositivo específico por ID",
        )

    def handle(self, *args, **options):
        mtx = MediaMTXAPI()
        existing = {p["name"] for p in mtx.list_paths()}
        self.stdout.write(f"Paths existentes en MediaMTX: {len(existing)}")

        devices = Device.objects.all()
        if options["device_id"]:
            devices = devices.filter(id=options["device_id"])

        synced = 0
        skipped = 0
        errors = 0

        for device in devices:
            if not device.username or not device.password:
                self.stdout.write(
                    f"  [{device.id}] {device.name} — sin credenciales, saltando"
                )
                skipped += 1
                continue

            self.stdout.write(
                f"  [{device.id}] {device.name} ({device.host})...", ending=" "
            )

            try:
                profiles_tokens = []
                stream_uris = []

                if device.stream_uris:
                    profiles_tokens = list(device.stream_uris.keys())
                    stream_uris = list(device.stream_uris.values())
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"OK (desde DB, {len(profiles_tokens)} perfiles)"
                        )
                    )
                else:
                    client = OnvifClient(
                        device.host, device.port, device.username, device.password
                    )
                    svc = MediaService(client)
                    profiles = svc.get_profiles()

                    for p in profiles:
                        uri = svc.get_stream_uri(
                            p["token"],
                            username=device.username,
                            password=device.password,
                        )
                        if uri:
                            profiles_tokens.append(p["token"])
                            stream_uris.append(uri)

                    self.stdout.write(
                        self.style.SUCCESS(
                            f"OK (desde ONVIF, {len(profiles_tokens)} perfiles)"
                        )
                    )

                mtx.ensure_camera_streams(device.id, profiles_tokens, stream_uris)
                synced += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"ERROR: {e}"))
                errors += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSincronización completada: {synced} ok, {skipped} saltados, {errors} errores"
            )
        )
