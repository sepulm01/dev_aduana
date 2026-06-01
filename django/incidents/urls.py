from django.urls import path
from incidents import views

urlpatterns = [
    path("incidents/types/", views.incident_type_list, name="incident_type_list"),
    path("incidents/types/create/", views.incident_type_create, name="incident_type_create"),
    path("incidents/types/<int:type_id>/edit/", views.incident_type_edit, name="incident_type_edit"),
    path("incidents/types/<int:type_id>/delete/", views.incident_type_delete, name="incident_type_delete"),
    path("incidents/", views.incident_list, name="incident_list"),
    path("api/incidents/<int:incident_id>/ack/", views.incident_ack, name="incident_ack"),
    path("incidents/dashboard/", views.incident_dashboard, name="incident_dashboard"),
]
