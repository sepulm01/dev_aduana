import os

from django.core.management.base import BaseCommand
from django.conf import settings

from clasificador.models import CropSello


class Command(BaseCommand):
    help = ("Indexa los crops de sello nuevos (*_sello.jpg) y retira "
            "los pendientes cuyo archivo fue purgado.")

    def handle(self, *args, **opts):
        dir_ = os.path.join(settings.MEDIA_ROOT, "fotos_b3")
        actuales = set()
        if os.path.isdir(dir_):
            for fn in os.listdir(dir_):
                if fn.endswith("_sello.jpg"):
                    actuales.add(fn)

        existentes = set(
            CropSello.objects.values_list("path", flat=True))
        nuevos = actuales - existentes
        CropSello.objects.bulk_create([
            CropSello(path=p, cam=1 if p.startswith("cam1_") else 2)
            for p in sorted(nuevos)
        ])

        CropSello.objects.filter(estado="pendiente").exclude(
            path__in=actuales).delete()

        self.stdout.write(
            f"[sync] {len(actuales)} crops, {len(nuevos)} nuevos")
