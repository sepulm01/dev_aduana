from django.urls import path
from live import views

urlpatterns = [
    path("<int:device_id>/", views.live_view, name="live_view"),
]
