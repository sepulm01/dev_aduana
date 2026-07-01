from django.conf import settings
from django.db import models


class Site(models.Model):
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SiteEscalationLevel(models.Model):
    site = models.ForeignKey(
        Site, on_delete=models.CASCADE, related_name="escalation_levels"
    )
    level = models.IntegerField()
    timeout_seconds = models.IntegerField()
    requires_ack = models.BooleanField(default=True)

    class Meta:
        ordering = ["site", "level"]
        unique_together = ["site", "level"]

    def __str__(self):
        return f"{self.site} L{self.level} ({self.timeout_seconds}s)"


class OperatorProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    phone_number = models.CharField(max_length=20, blank=True, default="")
    cargo = models.CharField(max_length=120, blank=True, default="")
    photo = models.ImageField(upload_to="profiles/", null=True, blank=True)
    escalation_level = models.IntegerField(default=1)
    sites = models.ManyToManyField(
        Site,
        blank=True,
        related_name="members",
    )

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        return f"{self.user.username} (L{self.escalation_level})"


class SiteMembership(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="site_memberships",
    )
    site = models.ForeignKey(
        Site, on_delete=models.CASCADE, related_name="memberships"
    )
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["user", "site"]
        ordering = ["-joined_at"]

    def __str__(self):
        return f"{self.user.username} @ {self.site.name}"
