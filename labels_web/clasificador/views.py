import io
import os
import zipfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView as AuthLoginView
from django.contrib.auth.views import LogoutView as AuthLogoutView
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clasificador.models import CropSello


class LoginView(AuthLoginView):
    pass


class LogoutView(LoginRequiredMixin, AuthLogoutView):
    pass


@login_required
def pendientes(request):
    crops = CropSello.objects.filter(estado="pendiente")
    pag = Paginator(crops, 48)
    pagina = pag.get_page(request.GET.get("p"))
    total_pend = pag.count
    total_hechas = CropSello.objects.filter(estado="procesado").count()
    return render(request, "pendientes.html", {
        "pagina": pagina,
        "total_pend": total_pend,
        "total_hechas": total_hechas,
    })


@login_required
@require_POST
def clasificar(request, pk):
    etiqueta = request.POST.get("etiqueta")
    if etiqueta not in ("con", "sin", "duda"):
        messages.error(request, "Etiqueta inválida.")
        return redirect("pendientes")
    with transaction.atomic():
        crop = get_object_or_404(
            CropSello.objects.select_for_update(), pk=pk)
        if crop.estado == "procesado":
            messages.error(
                request,
                f"Ya fue procesado por {crop.usuario} "
                f"({crop.get_etiqueta_display()}).")
            return redirect("pendientes")
        crop.estado = "procesado"
        crop.etiqueta = etiqueta
        crop.usuario = request.user
        crop.guardado_at = timezone.now()
        crop.save()
    messages.success(request, "Clasificación guardada.")
    return redirect("pendientes")


@login_required
def procesados(request):
    crops = CropSello.objects.filter(estado="procesado").order_by(
        "-guardado_at")
    pag = Paginator(crops, 60)
    pagina = pag.get_page(request.GET.get("p"))
    return render(request, "procesados.html", {"pagina": pagina})


@login_required
def exportar(request):
    if not request.user.is_staff:
        messages.error(request, "Sin permisos para exportar.")
        return redirect("pendientes")
    crops = CropSello.objects.filter(
        estado="procesado").exclude(etiqueta="duda")
    buf = io.BytesIO()
    incluidos = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for c in crops:
            if not c.existe:
                continue
            carpeta = "con_sello" if c.etiqueta == "con" else "sin_sello"
            z.write(c.ruta_disco, os.path.join(carpeta, c.path))
            incluidos += 1
    buf.seek(0)
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    nombre = timezone.now().strftime("sellos_clasificados_%Y%m%d_%H%M.zip")
    resp["Content-Disposition"] = f'attachment; filename="{nombre}"'
    return resp
