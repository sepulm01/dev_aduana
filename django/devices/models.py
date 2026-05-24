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
    failure_count = models.IntegerField(default=0)
    stream_uris = models.JSONField(blank=True, default=dict)
    camera_specs = models.JSONField(blank=True, default=dict)
    default_profile_token = models.CharField(max_length=120, blank=True, default="")
    motion_active = models.BooleanField(default=False)
    event_listener_enabled = models.BooleanField(default=False)
    deepstream_enabled = models.BooleanField(default=False)

    class Meta:
        ordering = ["-discovered_at"]

    def __str__(self):
        return f"{self.name} ({self.host})"


class IVSRule(models.Model):
    RULE_TYPES = [
        ("CrossLine", "Cross Line (Tripwire)"),
        ("CrossRegion", "Cross Region (Intrusion)"),
        ("SmartMotion", "Smart Motion"),
        ("ParkingDetection", "Parking Detection"),
        ("VideoMotion", "Video Motion"),
    ]
    DIRECTION_CHOICES = [
        ("A->B", "A → B"),
        ("B->A", "B → A"),
        ("Both", "Both"),
    ]

    device = models.ForeignKey(
        Device, on_delete=models.CASCADE, related_name="ivs_rules"
    )
    index = models.IntegerField(default=0)
    name = models.CharField(max_length=120, blank=True, default="")
    rule_type = models.CharField(max_length=40, choices=RULE_TYPES, default="CrossLine")
    enable = models.BooleanField(default=True)
    direction = models.CharField(
        max_length=10, choices=DIRECTION_CHOICES, default="Both"
    )
    detect_line = models.TextField(blank=True, default="")
    detect_region = models.TextField(blank=True, default="")
    event_handler = models.JSONField(blank=True, default=dict)
    camera_rule_id = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["device", "index"]
        unique_together = [["device", "index"]]

    def __str__(self):
        return f"{self.device.name} - {self.name or self.rule_type} (#{self.index})"


class DeviceEvent(models.Model):
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="events")
    code = models.CharField(max_length=80)
    action = models.CharField(max_length=20)
    index = models.IntegerField(default=0)
    data = models.JSONField(blank=True, default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name_plural = "Device events"

    def __str__(self):
        return f"{self.device.name} - {self.code} ({self.action}) @ {self.timestamp}"


class AnalyticsPreset(models.Model):
    device = models.ForeignKey(
        Device, on_delete=models.CASCADE, related_name="analytics_presets"
    )
    preset_token = models.CharField(max_length=120)
    preset_name = models.CharField(max_length=120, blank=True, default="")
    shapes = models.JSONField(blank=True, default=list)
    ptz_position = models.JSONField(blank=True, default=dict)
    snapshot = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["device", "preset_token"]
        unique_together = [["device", "preset_token"]]

    def __str__(self):
        return f"{self.device.name} - {self.preset_name or self.preset_token}"
