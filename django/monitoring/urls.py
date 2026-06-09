from django.urls import path

from monitoring import views

urlpatterns = [
    path("", views.dashboard, name="monitoring_dashboard"),
    path("api/", views.api_metrics, name="monitoring_api"),
]
