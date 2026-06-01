from django.urls import path
from notifications import views

urlpatterns = [
    path("notifications/channels/", views.channel_list, name="notification_channel_list"),
    path("notifications/channels/create/", views.channel_create, name="notification_channel_create"),
    path("notifications/channels/<int:channel_id>/edit/", views.channel_edit, name="notification_channel_edit"),
    path("notifications/channels/<int:channel_id>/delete/", views.channel_delete, name="notification_channel_delete"),
    path("notifications/rules/", views.rule_list, name="notification_rule_list"),
    path("notifications/rules/create/", views.rule_create, name="notification_rule_create"),
    path("notifications/rules/<int:rule_id>/edit/", views.rule_edit, name="notification_rule_edit"),
    path("notifications/rules/<int:rule_id>/delete/", views.rule_delete, name="notification_rule_delete"),
]
