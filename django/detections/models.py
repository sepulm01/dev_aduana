from django.db import models
from pgvector.django import VectorField, IvfflatIndex


class Detection(models.Model):
    device = models.ForeignKey("devices.Device", on_delete=models.CASCADE)

    event_code = models.CharField(max_length=64, default="DeepStreamDetection")
    object_id = models.BigIntegerField(db_index=True)
    class_label = models.CharField(max_length=32)
    confidence = models.FloatField()

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

    def __str__(self):
        return f"{self.class_label}#{self.object_id} @ {self.device}"
