from django.db import models


class MetricSnapshot(models.Model):
    source = models.CharField(max_length=50)
    device_id = models.IntegerField(null=True, blank=True)
    data = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["source", "created_at"]),
        ]

    def __str__(self):
        return f"{self.source} @ {self.created_at}"
