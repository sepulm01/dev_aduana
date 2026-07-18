from django.db import models


class ContainerEvent(models.Model):
    SEAL_CHOICES = [
        ("con_sello", "Con Sello"),
        ("sin_sello", "Sin Sello"),
        ("indeterminado", "Indeterminado"),
        ("processing", "Procesando"),
    ]

    container_code = models.CharField(max_length=32, blank=True, default="")
    seal_status = models.CharField(
        max_length=16, default="processing", choices=SEAL_CHOICES
    )
    seal_confidence = models.FloatField(default=0.0)
    timestamp_start = models.DateTimeField(db_index=True)
    timestamp_end = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp_start"]

    def __str__(self):
        code = self.container_code or "???"
        return f"Evento {self.id} — {code} — {self.seal_status}"


class ContainerDetection(models.Model):
    CLASS_CHOICES = [
        (0, "con_sello"),
        (1, "sin_sello"),
        (2, "cont data"),
        (3, "container cod"),
    ]
    CLASS_LABELS = {c[0]: c[1] for c in CLASS_CHOICES}

    event = models.ForeignKey(
        ContainerEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="detections",
    )
    device = models.ForeignKey(
        "devices.Device", on_delete=models.CASCADE, related_name="container_detections"
    )
    source_id = models.IntegerField(default=0)
    class_id = models.IntegerField(choices=CLASS_CHOICES)
    object_id = models.BigIntegerField(default=0)
    frame_num = models.BigIntegerField(default=0)
    confidence = models.FloatField(default=0.0)
    bbox_left = models.FloatField()
    bbox_top = models.FloatField()
    bbox_width = models.FloatField()
    bbox_height = models.FloatField()
    dominant_color_h = models.FloatField(null=True, blank=True)
    dominant_color_s = models.FloatField(null=True, blank=True)
    dominant_color_v = models.FloatField(null=True, blank=True)
    roi_name = models.CharField(max_length=32, blank=True, default="")
    timestamp = models.DateTimeField(db_index=True)
    crop = models.ImageField(upload_to="crops/%Y/%m/%d/", max_length=255)
    ocr_text = models.CharField(max_length=64, blank=True, default="")
    ocr_confidence = models.FloatField(null=True, blank=True)
    ocr_texts = models.JSONField(default=list, blank=True)
    ocr_processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["device", "timestamp"]),
            models.Index(fields=["event", "class_id"]),
        ]

    @property
    def class_label(self):
        return self.CLASS_LABELS.get(self.class_id, "unknown")

    def __str__(self):
        return (
            f"Det {self.id} — {self.class_label} "
            f"(conf={self.confidence:.2f}) — src={self.source_id}"
        )
