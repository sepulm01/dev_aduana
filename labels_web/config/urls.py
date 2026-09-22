from django.contrib import admin
from django.urls import path

from clasificador import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.pendientes, name="pendientes"),
    path("procesados/", views.procesados, name="procesados"),
    path("clasificar/<int:pk>/", views.clasificar, name="clasificar"),
    path("exportar/", views.exportar, name="exportar"),
    path("accounts/login/",
         views.LoginView.as_view(template_name="login.html"),
         name="login"),
    path("accounts/logout/", views.LogoutView.as_view(), name="logout"),
]
