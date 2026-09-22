import os

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class CropSello(models.Model):
    """Un crop de sello (recorte del keypoint 3) pendiente de clasificar.

    La decision del usuario se refiere SOLO al keypoint 3.
    """

    ETIQUETAS = [
        ("con", "CON SELLO"),
        ("sin", "SIN SELLO"),
        ("duda", "DUDA"),
    ]

    path = models.CharField(max_length=255, unique=True)
    cam = models.PositiveSmallIntegerField(default=0, blank=True)
    estado = models.CharField(max_length=12, default="pendiente")
    etiqueta = models.CharField(
        max_length=6, null=True, blank=True, choices=ETIQUETAS)
    usuario = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL)
    guardado_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["path"]

    @property
    def archivo(self):
        return os.path.join("fotos_b3", self.path)

    @property
    def ruta_disco(self):
        return os.path.join(settings.MEDIA_ROOT, "fotos_b3", self.path)

    @property
    def existe(self):
        return os.path.exists(self.ruta_disco)

    def __str__(self):
        return self.path
