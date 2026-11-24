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
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils import timezone


class PrivateTemplateStorage(FileSystemStorage):
    """Private base-image storage (handoff §19): never served by any web
    route; content reaches workhorses only through the agent API."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("location", str(settings.PRIVATE_TEMPLATES_DIR))
        super().__init__(*args, **kwargs)


class MachineImage(models.Model):
    """A versioned base machine image the operator authors (handoff §10, §13).

    One artifact per (role, version): the pinned domain.xml (with @NAME@,
    @DISK@, @VNC_PORT@, @LAB_NET@ placeholders) plus the uploaded base disk
    (qcow2/ISO). Files live in private storage on the controller and are
    staged to workhorses via the agent's template vocabulary — allocation
    never silently substitutes a different version (§13).
    """

    class Role(models.TextChoices):
        ATTACKER = "attacker", "Attacker (Kali)"
        TARGET = "target", "Target"

    role = models.CharField(max_length=12, choices=Role.choices)
    version = models.CharField(max_length=80)  # machine-readable, used as a directory name
    notes = models.CharField(max_length=500, blank=True)
    domain_xml = models.TextField()  # must carry the four placeholders
    disk_file = models.FileField(storage=PrivateTemplateStorage(), upload_to="images/", null=True, blank=True)
    disk_sha256 = models.CharField(max_length=64, blank=True)
    disk_size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "labs_machine_image"
        unique_together = [("role", "version")]

    def __str__(self):
        return f"{self.role}/{self.version}"


class Host(models.Model):
    """Workhorse host inventory (handoff §20). The agent reports capacity and
    heartbeat; admission respects the owner's 20% free-resource reserve."""

    name = models.CharField(max_length=100, unique=True)
    online = models.BooleanField(default=True)
    capacity_slots = models.PositiveSmallIntegerField(default=1)
    token_hash = models.CharField(max_length=64, blank=True)  # sha256 of the agent's bearer token.
    agent_version = models.CharField(max_length=50, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    free_disk_gb = models.FloatField(default=0)
    total_disk_gb = models.FloatField(default=0)
    free_ram_gb = models.FloatField(default=0)
    total_ram_gb = models.FloatField(default=0)
    running_labs = models.PositiveSmallIntegerField(default=0)
    management_ip = models.GenericIPAddressField(null=True, blank=True)  # Reported by the agent heartbeat.
    inventory = models.JSONField(default=dict, blank=True)  # {role: {version: {"<file>": size}}} from the heartbeat.

    class Meta:
        db_table = "labs_host"

    def __str__(self):
        return self.name

    @property
    def heartbeat_stale(self):
        """No heartbeat for 90 seconds means the agent link is down. Guests are
        NOT assumed stopped (§18): only admissions are blocked."""
        if self.last_heartbeat is None:
            return True
        return (timezone.now() - self.last_heartbeat).total_seconds() > 90

    @property
    def admits(self):
        """20% reserve (owner decision 2026-11-14): stop new admissions when
        free RAM or disk drops below 20% of total capacity."""
        if self.total_disk_gb <= 0 or self.total_ram_gb <= 0:
            return self.online and not self.heartbeat_stale  # inventory unknown yet
        disk_ok = self.free_disk_gb / self.total_disk_gb >= 0.2
        ram_ok = self.free_ram_gb / self.total_ram_gb >= 0.2
        return self.online and not self.heartbeat_stale and disk_ok and ram_ok


class MachineBuild(models.Model):
    """An interactive template build (one scratch VM per host, §10).

    The operator boots a scratch guest from a staged base, customizes it
    (desktop console + SSH command batches), then captures its disk as a new
    versioned template. Captures are HOST-LOCAL: the sha is computed on the
    workhorse, so the MachineImage record has no controller-side file until
    someone uploads one for fleet-wide distribution."""

    class State(models.TextChoices):
        STARTING = "starting", "Booting"
        READY = "ready", "Ready (customize via console / SSH)"
        CAPTURING = "capturing", "Capturing (conversion in progress)"
        CAPTURED = "captured", "Captured"
        DISCARDING = "discarding", "Discarding"
        DISCARDED = "discarded", "Discarded"
        FAILED = "failed", "Failed"

    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="builds")
    role = models.CharField(max_length=12, choices=MachineImage.Role.choices)
    base_version = models.CharField(max_length=80, blank=True)
    version = models.CharField(max_length=80)
    ssh_user = models.CharField(max_length=50, default="root")
    state = models.CharField(max_length=12, choices=State.choices, default=State.STARTING)
    vnc_port = models.CharField(max_length=6, blank=True)
    guest_ip = models.CharField(max_length=45, blank=True)
    last_output = models.JSONField(default=dict, blank=True)
    history = models.JSONField(default=list, blank=True)  # recent exec batches, newest first
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "labs_machine_build"

    def __str__(self):
        return f"build {self.role}/{self.version} on {self.host.name} ({self.state})"


class HostCommand(models.Model):
    """Durable command (handoff §18): stable id, target, expected generation,
    status, and result. Retries never duplicate destructive operations."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    op_id = models.CharField(max_length=100, unique=True)
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="commands")
    lab = models.ForeignKey("LabInstance", null=True, blank=True, on_delete=models.SET_NULL, related_name="commands")
    operation = models.CharField(max_length=30)  # allocate / save / reset-target / reset-full
    payload = models.JSONField(default=dict, blank=True)
    expected_generation = models.PositiveBigIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    acknowledged_deadline = models.DateTimeField(null=True, blank=True)  # Host-local lease enforcement.

    class Meta:
        db_table = "labs_host_command"
        indexes = [models.Index(fields=["host", "status"])]

    def __str__(self):
        return f"{self.operation}:{self.op_id}:{self.status}"


class LabPreset(models.Model):
    """A named, preconfigured lab: pinned attacker/target versions + sizing.

    Challenges pick a preset from a dropdown (or none). Pinning COPIES the
    preset's values into the challenge's LabTemplate, so a lab always runs
    what it was pinned with — editing or deleting a preset never rewrites
    existing challenges, and presets can evolve independently."""

    name = models.CharField(max_length=100, unique=True)
    attacker_version = models.CharField(max_length=100, blank=True, default="")
    target_version = models.CharField(max_length=100, blank=True, default="")
    attacker_ram_mb = models.PositiveIntegerField(default=4096)
    attacker_vcpu = models.PositiveSmallIntegerField(default=2)
    attacker_disk_gb = models.PositiveIntegerField(default=20)
    target_ram_mb = models.PositiveIntegerField(default=2048)
    target_vcpu = models.PositiveSmallIntegerField(default=1)
    target_disk_gb = models.PositiveIntegerField(default=10)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "labs_lab_preset"
        ordering = ("name",)

    def __str__(self):
        return self.name

    def matches(self, template):
        """True when a challenge's pin is exactly this preset's values."""
        return bool(template) and (
            template.attacker_version == self.attacker_version
            and template.target_version == self.target_version
            and template.attacker_ram_mb == self.attacker_ram_mb
            and template.attacker_vcpu == self.attacker_vcpu
            and template.attacker_disk_gb == self.attacker_disk_gb
            and template.target_ram_mb == self.target_ram_mb
            and template.target_vcpu == self.target_vcpu
            and template.target_disk_gb == self.target_disk_gb
        )


class LabTemplate(models.Model):
    """Challenge → pinned attacker/target template versions."""
    challenge = models.OneToOneField("content.Challenge", null=True, blank=True, on_delete=models.SET_NULL, related_name="lab_template")
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
    """A user's lab for one challenge (handoff §18 state machine)."""

    HOLDING_STATES = ("reserved", "starting", "restoring", "running", "saving", "unavailable", "recovery")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="labs")
    challenge = models.ForeignKey("content.Challenge", on_delete=models.PROTECT, related_name="labs")
    template = models.ForeignKey(LabTemplate, null=True, on_delete=models.PROTECT, related_name="instances")
    host = models.ForeignKey(Host, null=True, on_delete=models.PROTECT, related_name="labs")
    state = models.CharField(max_length=20, default="saved")
    generation = models.PositiveBigIntegerField(default=1)
    target_generation = models.PositiveBigIntegerField(default=1)
    saved_state_ref = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "labs_lab_instance"
        constraints = [
            models.UniqueConstraint(fields=("user", "challenge"), name="uniq_lab_user_challenge"),
            # One active allocation per user overall (handoff §11); enforced by
            # the database as well as the service layer.
            models.UniqueConstraint(
                fields=("user",),
                condition=models.Q(state__in=("reserved", "starting", "restoring", "running", "saving", "unavailable", "recovery")),
                name="uniq_one_active_allocation",
            ),
        ]


class Lease(models.Model):
    lab = models.ForeignKey(LabInstance, on_delete=models.CASCADE, related_name="leases")
    ready_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revision = models.PositiveBigIntegerField(default=1)
    active = models.BooleanField(default=True)
    warned_five = models.BooleanField(default=False)
    warned_one = models.BooleanField(default=False)

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
