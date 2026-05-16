from django.db import models


class Device(models.Model):
    name = models.CharField(max_length=120)
    host = models.CharField(max_length=120)
    port = models.IntegerField(default=80)
    username = models.CharField(max_length=80, blank=True, default="")
    password = models.CharField(max_length=80, blank=True, default="")
    manufacturer = models.CharField(max_length=120, blank=True, default="")
    model = models.CharField(max_length=120, blank=True, default="")
    firmware = models.CharField(max_length=120, blank=True, default="")
    serial_number = models.CharField(max_length=120, blank=True, default="")
    hardware_id = models.CharField(max_length=120, blank=True, default="")
    xaddrs = models.TextField(blank=True, default="")
    scopes = models.TextField(blank=True, default="")
    is_online = models.BooleanField(default=False)
    discovered_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    stream_uris = models.JSONField(blank=True, default=dict)
    camera_specs = models.JSONField(blank=True, default=dict)
    default_profile_token = models.CharField(max_length=120, blank=True, default="")
    motion_active = models.BooleanField(default=False)

    class Meta:
        ordering = ["-discovered_at"]

    def __str__(self):
        return f"{self.name} ({self.host})"
