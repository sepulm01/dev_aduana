from django.db import models
from django.conf import settings
from pgvector.django import VectorField, IvfflatIndex


class IdentityGroup(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    detection_count = models.IntegerField(default=1)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    info = models.JSONField(blank=True, default=dict)

    class Meta:
        ordering = ["-detection_count"]

    def __str__(self):
        return f"Identity #{self.pk} ({self.detection_count} visits)"


class IdentityLog(models.Model):
    ACTION_CHOICES = [
        ("reassign", "Reassign Detection"),
        ("merge", "Merge Groups"),
    ]
    identity_group = models.ForeignKey(
        IdentityGroup, on_delete=models.CASCADE, related_name="logs"
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    details = models.JSONField(default=dict)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.action} on Identity #{self.identity_group_id}"


class Detection(models.Model):
    device = models.ForeignKey("devices.Device", on_delete=models.CASCADE)
    identity_group = models.ForeignKey(
        IdentityGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="detections",
    )

    event_code = models.CharField(max_length=64, default="DeepStreamDetection")
    object_id = models.BigIntegerField(db_index=True)
    class_label = models.CharField(max_length=32)

    bbox_left = models.FloatField()
    bbox_top = models.FloatField()
    bbox_width = models.FloatField()
    bbox_height = models.FloatField()

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    crop = models.ImageField(
        upload_to="detections/crops/%Y/%m/%d/", null=True, blank=True
    )
    embedding = VectorField(dimensions=512, null=True, blank=True)
    landmarks = models.JSONField(null=True, blank=True)
    quality_score = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["device", "object_id"]),
            IvfflatIndex(
                name="det_embedding_cosine_idx",
                fields=["embedding"],
                lists=100,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    @property
    def has_embedding(self):
        return self.embedding is not None and len(self.embedding) > 0

    def __str__(self):
        return f"{self.class_label}#{self.object_id} @ {self.device}"
