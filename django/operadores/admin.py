from django.contrib import admin

from operadores.models import Site, SiteEscalationLevel, OperatorProfile, SiteMembership


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active"]


@admin.register(SiteEscalationLevel)
class SiteEscalationLevelAdmin(admin.ModelAdmin):
    list_display = ["site", "level", "timeout_seconds", "requires_ack"]


@admin.register(OperatorProfile)
class OperatorProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "cargo", "escalation_level", "phone_number"]


@admin.register(SiteMembership)
class SiteMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "site", "is_active"]
