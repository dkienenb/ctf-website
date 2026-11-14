"""Lab data model placeholders (handoff §10–§13, §20).

Schema only for now: allocation, queue, leases, and recovery coordination are
implemented later in the build sequence (§24 steps 2–3, 6). Invariants already
fixed here:
- One active lab allocation per user (enforced when services land).
- One outstanding queue request per user.
- Saved labs stay pinned to their original template versions; pinned versions
  are never silently replaced.
- VM sizes default to the owner decision: attacker 4 GB/2 vCPU/20 GB,
  target 2 GB/1 vCPU/10 GB.
"""
from django.conf import settings
from django.db import models


class LabTemplate(models.Model):
    """Challenge → pinned attacker/target template versions."""

    challenge = models.OneToOneField("content.Challenge", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    attacker_version = models.CharField(max_length=100)
    target_version = models.CharField(max_length=100)
    attacker_ram_mb = models.PositiveIntegerField(default=4096)
    attacker_vcpu = models.PositiveSmallIntegerField(default=2)
    attacker_disk_gb = models.PositiveIntegerField(default=20)
    target_ram_mb = models.PositiveIntegerField(default=2048)
    target_vcpu = models.PositiveSmallIntegerField(default=1)
    target_disk_gb = models.PositiveIntegerField(default=10)
    network_requirements = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "labs_lab_template"

    def __str__(self):
        return f"{self.attacker_version}/{self.target_version}"


class LabInstance(models.Model):
    """A user's lab for one challenge (handoff state machine lands with services)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="labs")
    challenge = models.ForeignKey("content.Challenge", on_delete=models.PROTECT, related_name="labs")
    template = models.ForeignKey(LabTemplate, null=True, on_delete=models.PROTECT, related_name="instances")
    host = models.CharField(max_length=100, blank=True)  # Original workhorse; placement resumes here initially.
    state = models.CharField(max_length=20, default="saved")  # Baseline state enumeration §18.
    generation = models.PositiveBigIntegerField(default=1)
    saved_state_ref = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "labs_lab_instance"
        constraints = [models.UniqueConstraint(fields=("user", "challenge"), name="uniq_lab_user_challenge")]


class Lease(models.Model):
    lab = models.ForeignKey(LabInstance, on_delete=models.CASCADE, related_name="leases")
    ready_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revision = models.PositiveBigIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "labs_lease"


class QueueEntry(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="queue_entries")
    challenge = models.ForeignKey("content.Challenge", on_delete=models.PROTECT, related_name="queue_entries")
    enqueued_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "labs_queue_entry"
        constraints = [models.UniqueConstraint(fields=("user", "active"), condition=models.Q(active=True), name="uniq_outstanding_request")]
