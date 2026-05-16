from django.urls import path
from ptz import views

urlpatterns = [
    path("<int:device_id>/move/", views.move, name="ptz_move"),
    path("<int:device_id>/status/", views.status, name="ptz_status"),
    path("<int:device_id>/preset/", views.preset, name="ptz_preset"),
]
