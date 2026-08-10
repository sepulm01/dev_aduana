from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("devices.urls")),
    path("aduana/", include("aduana.urls")),
    path("live/", include("live.urls")),
    path("", include("operadores.urls")),
    path("monitoring/", include("monitoring.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
