from django.db import models


class IncidentType(models.Model):
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    auto_resolve_seconds = models.IntegerField(default=0)
    dedup_window_seconds = models.IntegerField(default=0)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class EscalationLevel(models.Model):
    incident_type = models.ForeignKey(
        IncidentType, on_delete=models.CASCADE, related_name="levels"
    )
    level = models.IntegerField()
    channel = models.ForeignKey(
        "notifications.NotificationChannel", on_delete=models.CASCADE
    )
    timeout_seconds = models.IntegerField()
    requires_ack = models.BooleanField(default=True)
    message_template = models.TextField(blank=True, default="")
    auto_actions = models.JSONField(blank=True, default=list)

    class Meta:
        ordering = ["incident_type", "level"]
        unique_together = ["incident_type", "level"]

    def __str__(self):
        return f"{self.incident_type} level {self.level}"


class Incident(models.Model):
    STATUS_CHOICES = [
        ("active", "Active"),
        ("acknowledged", "Acknowledged"),
        ("resolved", "Resolved"),
        ("expired", "Expired"),
    ]

    incident_type = models.ForeignKey(IncidentType, on_delete=models.CASCADE)
    device = models.ForeignKey(
        "devices.Device", on_delete=models.CASCADE, related_name="incidents"
    )
    rule = models.ForeignKey(
        "notifications.NotificationRule",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    event_data = models.JSONField(blank=True, default=dict)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    current_level = models.IntegerField(default=1)
    acknowledged_by = models.CharField(max_length=120, blank=True, default="")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    level_started_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.incident_type} @ {self.device} [{self.status}]"


class IncidentLog(models.Model):
    incident = models.ForeignKey(
        Incident, on_delete=models.CASCADE, related_name="logs"
    )
    level = models.IntegerField()
    action = models.CharField(max_length=40)
    success = models.BooleanField(default=True)
    detail = models.JSONField(blank=True, default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"Incident #{self.incident_id} L{self.level} {self.action}"
