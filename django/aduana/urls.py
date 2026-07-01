from django.urls import path

from aduana import views

urlpatterns = [
    path("", views.dashboard, name="aduana_dashboard"),
    path("<int:event_id>/", views.event_detail, name="aduana_event_detail"),
]
