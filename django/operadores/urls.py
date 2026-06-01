from django.urls import path
from operadores import views

urlpatterns = [
    path("operadores/sites/", views.site_list, name="operador_site_list"),
    path("operadores/sites/create/", views.site_create, name="operador_site_create"),
    path("operadores/sites/<int:site_id>/edit/", views.site_edit, name="operador_site_edit"),
    path("operadores/sites/<int:site_id>/delete/", views.site_delete, name="operador_site_delete"),
    path("operadores/profile/", views.profile_view, name="operador_profile"),
    path("operadores/profile/edit/", views.profile_edit, name="operador_profile_edit"),
    path("operadores/", views.operator_list, name="operador_list"),
    path("operadores/<int:user_id>/edit/", views.operator_edit, name="operador_edit"),
    path("api/devices/<int:device_id>/assign-site/", views.device_assign_site, name="device_assign_site"),
]
