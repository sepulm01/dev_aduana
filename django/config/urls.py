from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("devices.urls")),
    path("aduana/", include("aduana.urls")),
    path("live/", include("live.urls")),
    path("", include("operadores.urls")),
    path("monitoring/", include("monitoring.urls")),
]
