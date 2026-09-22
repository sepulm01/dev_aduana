from django.contrib import admin

from clasificador.models import CropSello


@admin.register(CropSello)
class CropSelloAdmin(admin.ModelAdmin):
    list_display = ("path", "cam", "estado", "etiqueta", "usuario",
                    "guardado_at")
    list_filter = ("estado", "etiqueta", "cam")
    search_fields = ("path",)
