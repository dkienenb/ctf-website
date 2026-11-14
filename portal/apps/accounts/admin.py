from django.contrib import admin

from .models import ApprovalDecision, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "discord_id", "cached_name", "status", "is_admin", "created_at")
    list_filter = ("status", "is_admin")
    search_fields = ("discord_id", "cached_name")
    readonly_fields = ("discord_id", "created_at", "updated_at", "last_login", "profile_refreshed_at")
    # Approvals flow through the ApprovalDecision ledger, never by editing rows silently.
    exclude = ("password", "groups", "user_permissions")


@admin.register(ApprovalDecision)
class ApprovalDecisionAdmin(admin.ModelAdmin):
    list_display = ("id", "discord_id", "decision", "actor", "created_at")
    list_filter = ("decision",)
    search_fields = ("discord_id",)
    readonly_fields = ("created_at",)
