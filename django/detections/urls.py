from django.urls import path

from . import views

urlpatterns = [
    path("detections/", views.detection_list, name="detection_list"),
    path("detections/<int:pk>/", views.detection_detail, name="detection_detail"),
    path("identities/", views.identity_list, name="identity_list"),
    path("identities/<int:pk>/", views.identity_detail, name="identity_detail"),
    path(
        "api/identities/<int:pk>/reassign/",
        views.identity_reassign,
        name="identity_reassign",
    ),
    path(
        "api/identities/<int:pk>/merge/",
        views.identity_merge,
        name="identity_merge",
    ),
]
