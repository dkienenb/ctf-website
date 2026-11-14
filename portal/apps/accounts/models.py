"""Accounts and approval (handoff §3, owner decisions 2026-11-14).

Identity rules enforced here:
- The stable Discord user ID is the external identity key; exactly one account
  per Discord ID.
- Approval state is a backend authorization condition, never a UI rule.
- Cached Discord name/avatar change only through an explicit refresh and never
  affect identity, ownership, approval, or progress.
- Approval decisions (including pre-approval) are keyed by Discord ID and do
  not require an existing User row.
- Rejected users reapply by authenticating again; nothing is erased.
"""
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, discord_id, cached_name="", cached_avatar="", **extra):
        if not discord_id or not str(discord_id).isdigit():
            raise ValueError("A numeric Discord ID is required.")
        user = self.model(
            discord_id=str(discord_id), cached_name=cached_name, cached_avatar=cached_avatar, **extra
        )
        user.save(using=self._db)
        return user

    def create_superuser(self, discord_id, cached_name="Admin", cached_avatar="", **extra):
        return self.create_user(
            discord_id, cached_name, cached_avatar, is_admin=True, is_staff=True, is_superuser=True, **extra
        )


class User(AbstractBaseUser, PermissionsMixin):
    """Competition account keyed by the stable Discord user ID."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        SUSPENDED = "suspended", "Suspended"
        REJECTED = "rejected", "Rejected"

    discord_id = models.CharField(max_length=25, unique=True, db_index=True)
    cached_name = models.CharField(max_length=100, blank=True)
    cached_avatar = models.CharField(max_length=500, blank=True)  # URL or reference
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    is_admin = models.BooleanField(default=False)  # Separate from approval (handoff §3).
    is_staff = models.BooleanField(default=False)  # Django admin access (bootstrap only).
    profile_refreshed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "discord_id"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        db_table = "accounts_user"
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.cached_name or self.discord_id} ({self.pk})"

    @property
    def is_approved(self):
        return self.status == self.Status.APPROVED


class ApprovalDecision(models.Model):
    """Ledger of approvals/pre-approvals keyed by Discord ID (supports pre-user approval)."""

    discord_id = models.CharField(max_length=25, db_index=True)
    decision = models.CharField(max_length=20)  # approve / suspend / reinstate / reject / preapprove
    actor = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_approval_decision"

    def __str__(self):
        return f"{self.decision}:{self.discord_id}"
