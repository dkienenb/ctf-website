"""Lab allocation, queue, leases, and durable host commands (handoff §11, §18).

Ports of the prototype's verified rules plus the production command flow:

- One active lab per user overall (also enforced by a database constraint).
- One saved lab per user/challenge; one outstanding queue request per user.
- Claims start the lease only when the agent reports both guests ready
  (§11 "lease start: when the instance/lab is ready"). Until then the lab
  holds a reservation in the `starting` state.
- Every host operation is a durable HostCommand with a stable op id and the
  expected lab generation; stale commands are rejected (§18).
- Expiry/release issue `save` commands; the state stays `saving` (capacity
  still held) until the agent confirms, then the queue promotes.
- Save failure keeps the reservation and marks `recovery required` with an
  admin alert (§18 operational matrix).
- Warnings at five minutes and one minute, deduplicated per lease revision.
- Admission respects the owner's 20% free-resource reserve and skips hosts
  with a stale agent heartbeat. A stale heartbeat never assumes guests
  stopped (§18).
"""
import base64
import hashlib
import re
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge
from portal.apps.submissions.services import ServiceError

from .models import Host, HostCommand, LabInstance, LabTemplate, Lease, MachineImage, QueueEntry

RESERVATION_SECONDS = 120
LEASE_SECONDS = 1800
WARNING_THRESHOLDS = ((300, "warned_five", "Lab lease: 5 minute(s) remaining."),
                      (60, "warned_one", "Lab lease: 1 minute(s) remaining."))
HOLDING = LabInstance.HOLDING_STATES
STALE_HEARTBEAT_SECONDS = 90


def _assert_approved(user):
    if not getattr(user, "pk", None):
        raise ServiceError("Sign in first.", 401)
    if user.status != User.Status.APPROVED:
        raise ServiceError("Approved access is required.", 403)


def _visible(challenge):
    now = timezone.now()
    if challenge.lifecycle != Challenge.Lifecycle.PUBLISHED or (challenge.go_live and challenge.go_live > now):
        raise ServiceError("Challenge is not available.", 404)


def _admitting_hosts():
    return [host for host in Host.objects.filter(online=True) if host.admits]


def _free_capacity(host):
    holding = LabInstance.objects.filter(state__in=HOLDING)
    return holding.count() < host.capacity_slots


def _promote():
    """Promote the oldest queued request when a host has a free slot.

    The host row is locked for the duration: two concurrent promotes must
    serialize on it, or both would observe free capacity and double-book
    the last slot (caught by the concurrency test)."""
    for host_id in [host.id for host in _admitting_hosts()]:
        host = Host.objects.select_for_update().get(pk=host_id)
        if not _free_capacity(host):
            continue
        entry = (
            QueueEntry.objects.select_for_update(skip_locked=True)
            .filter(active=True)
            .order_by("enqueued_at", "id")
            .first()
        )
        if not entry:
            return
        entry.active = False
        entry.claimed_at = timezone.now()
        entry.save(update_fields=("active", "claimed_at"))
        lab, _ = LabInstance.objects.get_or_create(
            user_id=entry.user_id, challenge_id=entry.challenge_id,
            defaults={"state": "saved"},
        )
        lab.state = "reserved"
        if not lab.host_id:
            lab.host = host
        lab.save()
        _notify(lab.user_id, "A lab slot is ready. Claim it within two minutes.")
        return


def _notify(user_id, message):
    from portal.apps.submissions.models import Notification

    Notification.objects.create(recipient_id=user_id, message=message)


def _issue_command(lab, operation, payload=None, deadline=None):
    command = HostCommand.objects.create(
        op_id=secrets.token_hex(16),
        host=lab.host,
        lab=lab,
        operation=operation,
        payload=payload or {},
        expected_generation=lab.generation,
        acknowledged_deadline=deadline,
    )
    return command


def request_lab(user, challenge_id):
    """Enqueue a lab request; may promote immediately when capacity exists."""
    _assert_approved(user)
    with transaction.atomic():
        try:
            challenge = Challenge.objects.get(pk=challenge_id)
        except Challenge.DoesNotExist:
            raise ServiceError("Challenge not found.", 404)
        _visible(challenge)
        if not _admitting_hosts():
            raise ServiceError("Workhorse unavailable. Saved state is retained; retry when it returns.", 503)
        holding = LabInstance.objects.filter(user=user, state__in=HOLDING).first()
        if holding:
            raise ServiceError("You already hold a lab allocation. Save and release it before requesting another.", 409)
        if QueueEntry.objects.filter(user=user, active=True).exists():
            raise ServiceError("You already have one outstanding queue request.", 409)
        lab = LabInstance.objects.filter(user=user, challenge=challenge).first()
        if not lab:
            template = getattr(challenge, "lab_template", None)
            try:
                lab = LabInstance.objects.create(user=user, challenge=challenge, template=template, state="saved")
            except IntegrityError:
                raise ServiceError("A saved lab already exists for this challenge.", 409)
        QueueEntry.objects.create(user=user, challenge=challenge)
        _promote()
        lab.refresh_from_db()
        if lab.state == "reserved":
            return {"message": "Lab slot reserved. Claim it to start your 30-minute lease."}
        return {"message": "Queued. Your saved lab is retained."}


def vnc_port_for(lab):
    """Deterministic per-lab VNC port: no collisions between concurrent labs
    on one host, derivable identically by agent and controller."""
    return 5900 + (lab.id % 1000)


def claim(user, challenge_id):
    """Start allocation: issue the durable `allocate` command. The lease starts
    only when the agent reports both guests ready."""
    _assert_approved(user)
    with transaction.atomic():
        lab = LabInstance.objects.select_for_update().filter(user=user, challenge_id=challenge_id).first()
        if lab is None:
            raise ServiceError("No lab exists for this challenge yet.", 404)
        entry = (
            QueueEntry.objects.filter(user=user, challenge=lab.challenge, active=False, claimed_at__isnull=False)
            .order_by("-id")
            .first()
        )
        deadline = entry.claimed_at + timedelta(seconds=RESERVATION_SECONDS) if entry else None
        if lab.state != "reserved" or not deadline or deadline < timezone.now():
            raise ServiceError("No valid reservation to claim.", 409)
        if not lab.host or not lab.host.admits:
            raise ServiceError("The workhorse became unavailable. Your saved lab is retained.", 503)
        lab.state = "starting"
        lab.save(update_fields=("state", "updated_at"))
        template = lab.template or getattr(lab.challenge, "lab_template", None)
        _issue_command(lab, "allocate", {
            "lab": lab.id,
            "challenge": lab.challenge_id,
            "vnc_port": vnc_port_for(lab),
            "attacker_version": template.attacker_version if template else "",
            "target_version": template.target_version if template else "",
            "attacker": {"ram_mb": template.attacker_ram_mb if template else 4096,
                          "vcpu": template.attacker_vcpu if template else 2,
                          "disk_gb": template.attacker_disk_gb if template else 20},
            "target": {"ram_mb": template.target_ram_mb if template else 2048,
                        "vcpu": template.target_vcpu if template else 1,
                        "disk_gb": template.target_disk_gb if template else 10},
        }, deadline=None)
    return {"message": "Allocating your lab pair. The 30-minute lease starts when both guests are ready."}


def refresh(user, challenge_id):
    _assert_approved(user)
    with transaction.atomic():
        lab = LabInstance.objects.select_for_update().filter(user=user, challenge_id=challenge_id).first()
        if lab is None:
            raise ServiceError("No lab exists for this challenge yet.", 404)
        lease = lab.leases.filter(active=True).order_by("-id").first()
        if lab.state != "running" or lease is None:
            raise ServiceError("Lab is not running.", 409)
        if QueueEntry.objects.exclude(user=user).filter(active=True).exists():
            raise ServiceError("Refresh is unavailable while another user is queued. Your current expiry is unchanged.", 409)
        lease.revision += 1
        lease.expires_at = timezone.now() + timedelta(seconds=LEASE_SECONDS)
        lease.warned_five = False
        lease.warned_one = False
        lease.save(update_fields=("revision", "expires_at", "warned_five", "warned_one"))
        # Acknowledge the new deadline to the host for local enforcement (§18).
        _issue_command(lab, "refresh-lease", {"expires_at": lease.expires_at.isoformat()},
                       deadline=lease.expires_at)
    return {"message": "Lease expiry set to 30 minutes from now."}


def release(user, challenge_id):
    _assert_approved(user)
    with transaction.atomic():
        lab = LabInstance.objects.select_for_update().filter(user=user, challenge_id=challenge_id).first()
        if lab is None:
            raise ServiceError("No lab exists for this challenge yet.", 404)
        if lab.state == "reserved":
            _save_lab_locally(lab, "Both simulated guests saved. Capacity released.")
            _promote()
        elif lab.state == "running":
            _start_save(lab)
        else:
            raise ServiceError("Lab cannot be released in its current state.", 409)
    return {"message": "Saving your lab. Capacity releases when the save completes."}


def _save_lab_locally(lab, message=None):
    """Nothing reached the host yet (or it is already stopped): close out locally."""
    lab.state = "saved"
    lab.save(update_fields=("state", "updated_at"))
    lab.leases.filter(active=True).update(active=False)
    if message:
        _notify(lab.user_id, message)


def _start_save(lab):
    """Transition running → saving and issue the durable save command (§12)."""
    lab.state = "saving"
    lab.save(update_fields=("state", "updated_at"))
    _issue_command(lab, "save", {"lease_expires_at": lab.leases.filter(active=True).order_by("-id").first().expires_at.isoformat() if lab.leases.filter(active=True).exists() else None})


def tick():
    """Expiry/claim-timeout processing, warnings, and queue promotion."""
    with transaction.atomic():
        now = timezone.now()
        for lab in LabInstance.objects.select_for_update().filter(state="running"):
            lease = lab.leases.filter(active=True).order_by("-id").first()
            if lease is None:
                _start_save(lab)
                continue
            remaining = (lease.expires_at - now).total_seconds()
            for threshold, flag, message in WARNING_THRESHOLDS:
                if remaining <= threshold and remaining > 0 and not getattr(lease, flag):
                    setattr(lease, flag, True)
                    lease.save(update_fields=(flag,))
                    _notify(lab.user_id, message)
            if lease.expires_at <= now:
                _start_save(lab)
                _notify(lab.user_id, "Lease expired. Your lab is being saved; access has ended.")
        for lab in LabInstance.objects.select_for_update().filter(state="reserved"):
            entry_claimed = QueueEntry.objects.filter(
                user=lab.user, challenge=lab.challenge, claimed_at__isnull=False, active=False
            ).order_by("-id").first()
            deadline = entry_claimed.claimed_at + timedelta(seconds=RESERVATION_SECONDS) if entry_claimed else None
            if deadline and deadline <= now:
                _save_lab_locally(lab, "Reservation expired. Your saved lab was retained.")
        _promote()


def complete_command(host, op_id, status, result):
    """Agent result ingestion. Idempotent; stale commands are rejected (§18).

    The stale-rejection marks the command FAILED in its own committed
    transaction so the agent never re-polls it."""
    with transaction.atomic():
        command = HostCommand.objects.select_for_update().filter(op_id=op_id).first()
        if command is None or command.host_id != host.id:
            raise ServiceError("Unknown command.", 404)
        if command.status in (HostCommand.Status.COMPLETED, HostCommand.Status.FAILED):
            return {"message": "Already recorded.", "status": command.status}
        stale = False
        if command.lab:
            lab = LabInstance.objects.select_for_update().get(pk=command.lab_id)
            if lab.generation != command.expected_generation:
                command.status = HostCommand.Status.FAILED
                command.result = {"error": "stale-generation"}
                command.save(update_fields=("status", "result", "updated_at"))
                stale = True
    if stale:
        raise ServiceError("Stale command from an earlier lab generation.", 409)

    with transaction.atomic():
        command = HostCommand.objects.select_for_update().get(op_id=op_id)
        command.status = HostCommand.Status.FAILED if status == "failed" else HostCommand.Status.COMPLETED
        command.result = result or {}
        command.save(update_fields=("status", "result", "updated_at"))
        if command.lab:
            _apply_command_result(command)
    return {"message": "Command recorded.", "status": command.status}


def _apply_command_result(command):
    lab = LabInstance.objects.select_for_update().get(pk=command.lab_id)
    operation, ok = command.operation, command.status == HostCommand.Status.COMPLETED
    if operation == "allocate":
        if ok:
            if lab.user.status != User.Status.APPROVED:
                # Suspension mid-allocation: never grant desktop access (§3).
                lab.state = "saved"
                lab.save(update_fields=("state", "updated_at"))
                _notify(lab.user_id, "Your account was suspended during allocation. The lab was not started.")
                return
            now = timezone.now()
            lab.state = "running"
            lab.save(update_fields=("state", "updated_at"))
            Lease.objects.create(lab=lab, ready_at=now, expires_at=now + timedelta(seconds=LEASE_SECONDS))
            _notify(lab.user_id, "Both guests are ready. Your 30-minute lease starts now.")
        else:
            lab.state = "saved"
            lab.save(update_fields=("state", "updated_at"))
            _notify(lab.user_id, "The workhorse could not start your lab. Your saved state is retained; request the lab again.")
    elif operation == "save":
        if ok:
            _save_lab_locally(lab)
            _notify(lab.user_id, "Lab saved. Both guests retain their state.")
            _promote()
        else:
            # Save incomplete: keep artifacts and the reservation, mark
            # recovery required, alert admins (§18).
            lab.state = "recovery"
            lab.save(update_fields=("state", "updated_at"))
            for admin in User.objects.filter(is_admin=True, status=User.Status.APPROVED):
                _notify(admin.id, f"Save failed for lab #{lab.id} on {lab.host.name}. Recovery required.")
            _notify(lab.user_id, "Your lab is kept and capacity is still held. An administrator will recover it.")
    elif operation in ("reset-target", "reset-full", "recover"):
        if ok:
            if operation == "reset-full":
                lab.generation += 1
                message = "Entire simulated lab reset. Both guests returned to their pinned templates."
            elif operation == "recover":
                message = ("Your saved target state could not be recovered. The target was restored to its pinned "
                           "starting template; attacker workspace, challenge progress, hint ownership, and score were preserved.")
            else:
                message = "Target reset. Your attacker workspace was preserved."
            if operation in ("reset-target", "recover"):
                lab.target_generation += 1
            lab.state = "saved"
            lab.save(update_fields=("state", "target_generation", "generation", "updated_at"))
            _notify(lab.user_id, message)
        else:
            lab.state = "recovery"
            lab.save(update_fields=("state", "updated_at"))
            for admin in User.objects.filter(is_admin=True, status=User.Status.APPROVED):
                _notify(admin.id, f"Reset failed for lab #{lab.id} on {lab.host.name}. Recovery required.")
    elif operation == "refresh-lease":
        if not ok:
            # The host could not enforce the refresh; do not pretend it is live (§18).
            _notify(lab.user_id, "The workhorse could not acknowledge your lease refresh. Your previous expiry stands.")


def request_admin_reset(user, challenge_id):
    """Players request an administrator reset; they never self-reset (§13)."""
    _assert_approved(user)
    challenge = Challenge.objects.get(pk=challenge_id)
    for admin in User.objects.filter(is_admin=True, status=User.Status.APPROVED):
        _notify(admin.id, f"User #{user.id} requested an admin reset for {challenge.title}.")
    _notify(user.id, "Reset request sent to administrators. Your current lab and progress were retained.")
    return {"message": "Administrator reset requested."}


def admin_reset(actor, user_id, challenge_id, scope):
    """Issue the durable reset/recovery command (§13). scope: target|full|recover."""
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    with transaction.atomic():
        lab = LabInstance.objects.select_for_update().get(user_id=user_id, challenge_id=challenge_id)
        if scope == "recover" and lab.state not in ("recovery", "saved", "unavailable"):
            raise ServiceError("Recovery applies to labs in the recovery or saved state.", 409)
        operation = {"target": "reset-target", "full": "reset-full", "recover": "recover"}[scope]
        command = _issue_command(lab, operation)
        DataRevision.bump()
    return command


def set_host_online(actor, host_id, online):
    from portal.apps.submissions.models import DataRevision

    with transaction.atomic():
        host = Host.objects.select_for_update().get(pk=host_id)
        host.online = online
        host.save(update_fields=("online",))
        if not online:
            for lab in LabInstance.objects.select_for_update().filter(state__in=("running", "reserved")):
                lab.state = "unavailable"
                lab.save(update_fields=("state", "updated_at"))
                _notify(lab.user_id, "Workhorse unavailable. Browser access is interrupted; saved-state references are retained.")
        else:
            for lab in LabInstance.objects.select_for_update().filter(state="unavailable"):
                _save_lab_locally(lab, "Workhorse returned. State retained; request a slot to resume. No template reset occurred.")
            _promote()
    DataRevision.bump()
    return host


# --- Machine template authoring (§10, §13) ------------------------------------

DOMAIN_PLACEHOLDERS = ("@NAME@", "@DISK@", "@VNC_PORT@", "@LAB_NET@")
TARGET_PLACEHOLDERS = ("@NAME@", "@DISK@", "@LAB_NET@")  # targets are never streamed
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
STAGE_CHUNK_BYTES = 2 * 1024 * 1024


def validate_domain_xml(domain_xml, role=None):
    """A pinned template must carry every placeholder the agent substitutes.
    Target templates are never streamed, so they need no @VNC_PORT@."""
    required = DOMAIN_PLACEHOLDERS if role != MachineImage.Role.TARGET else TARGET_PLACEHOLDERS
    problems = [p for p in required if p not in domain_xml]
    if problems:
        raise ServiceError(
            "domain.xml is missing required placeholders: " + ", ".join(problems), 400
        )


def validate_version(version):
    """Version names become directory names on workhorses; keep them safe."""
    if not _SAFE_NAME_RE.fullmatch(version or ""):
        raise ServiceError(
            "Version must start with a letter or digit and use only letters, digits, dots, dashes, underscores.", 400
        )


def save_machine_image(actor, *, role, version, domain_xml, notes="", disk=None):
    """Create or replace a machine image artifact. Replacing the disk file
    invalidates the recorded hash; re-distribute before relying on it (§13:
    no silent substitution — the explicit distribute action is the pin)."""
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    if role not in MachineImage.Role.values:
        raise ServiceError("Role must be attacker or target.", 400)
    validate_version(version)
    validate_domain_xml(domain_xml or "", role)

    with transaction.atomic():
        image, created = MachineImage.objects.select_for_update().get_or_create(
            role=role, version=version,
            defaults={"domain_xml": domain_xml, "notes": notes, "uploaded_by": actor},
        )
        if not created:
            image.domain_xml = domain_xml
            if notes:
                image.notes = notes
        image.uploaded_by = actor
        if disk is not None:
            if image.disk_file:
                image.disk_file.delete(save=False)
            image.disk_file.save(f"{role}-{version}-disk-template", disk, save=False)
            image.disk_file.open("rb")
            hasher = hashlib.sha256()
            for block in iter(lambda: image.disk_file.read(1024 * 1024), b""):
                hasher.update(block)
            image.disk_file.close()
            image.disk_sha256 = hasher.hexdigest()
            image.disk_size = image.disk_file.size
        image.save()
        DataRevision.bump()
    return image, created


def delete_machine_image(actor, image_id):
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    try:
        image = MachineImage.objects.get(pk=image_id)
    except MachineImage.DoesNotExist:
        raise ServiceError("Machine image not found.", 404)
    referenced = (
        LabTemplate.objects.filter(attacker_version=image.version).exists()
        if image.role == MachineImage.Role.ATTACKER
        else LabTemplate.objects.filter(target_version=image.version).exists()
    )
    if referenced:
        raise ServiceError(
            "This version is pinned by at least one challenge template. Unassign it first.", 409
        )
    if image.disk_file:
        image.disk_file.delete(save=False)
    image.delete()
    DataRevision.bump()


def distribute_machine_image(actor, image_id):
    """Stage a pinned image onto every workhorse via the durable command
    queue (chunked template-put commands; op ids derive from the content
    hash so retries and re-distributions are idempotent)."""
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    try:
        image = MachineImage.objects.get(pk=image_id)
    except MachineImage.DoesNotExist:
        raise ServiceError("Machine image not found.", 404)
    if not image.disk_file:
        raise ServiceError("This image has no base disk uploaded yet.", 409)
    hosts = list(Host.objects.all())
    if not hosts:
        raise ServiceError("No workhorses are registered.", 409)

    image.disk_file.open("rb")
    payload_hasher = hashlib.sha256()
    chunks = []
    while True:
        block = image.disk_file.read(STAGE_CHUNK_BYTES)
        if not block:
            break
        payload_hasher.update(block)
        chunks.append(base64.b64encode(block).decode("ascii"))
    image.disk_file.close()
    staged_sha = payload_hasher.hexdigest()
    if staged_sha != image.disk_sha256:
        image.disk_sha256 = staged_sha
        image.save(update_fields=("disk_sha256",))

    xml_bytes = image.domain_xml.encode("utf-8")
    xml_sha = hashlib.sha256(xml_bytes).hexdigest()
    issued = 0

    def stage(host, op_id, operation, payload):
        """Idempotent issue: existing FAILED commands return to pending so
        a re-distribution retries them; COMPLETED ones are left alone."""
        command, created = HostCommand.objects.get_or_create(
            op_id=op_id, defaults={
                "host": host, "operation": operation, "payload": payload,
            },
        )
        if not created and command.status == HostCommand.Status.FAILED:
            command.status = HostCommand.Status.PENDING
            command.result = {}
            command.save(update_fields=("status", "result", "updated_at"))
        return command

    for host in hosts:
        blob = base64.b64encode(xml_bytes).decode("ascii")
        # The XML carries ITS OWN hash: the agent verifies every file against
        # the sha of its own content, not the disk's (caught live: domain.xml
        # was rejected on every transfer because it rode the disk's sha).
        stage(host, f"tpl-{xml_sha[:16]}-{host.id}-domain",
              "template-put", {
                  "role": image.role, "version": image.version,
                  "file": "domain.xml", "seq": 1, "total": 1,
                  "data_b64": blob, "sha256": xml_sha,
              })
        issued += 1
        for seq, chunk in enumerate(chunks, start=1):
            stage(host, f"tpl-{staged_sha[:16]}-{host.id}-disk-{seq}",
                  "template-put", {
                      "role": image.role, "version": image.version,
                      "file": "disk-template", "seq": seq, "total": len(chunks),
                      "data_b64": chunk, "sha256": staged_sha,
                  })
            issued += 1
        stage(host, f"tpl-{staged_sha[:16]}-{xml_sha[:8]}-{host.id}-done",
              "template-verify", {
                  "role": image.role, "version": image.version,
                  "sha256": staged_sha, "size": image.disk_size,
                  "xml_sha256": xml_sha,
              })
        issued += 1
    DataRevision.bump()
    return {"message": f"Staging {image.role}/{image.version} to {len(hosts)} workhorse(s) ({issued} commands).",
            "hosts": len(hosts), "commands": issued}


def lab_template_save(actor, challenge_id, *, attacker_version, target_version,
                      attacker_ram_mb, attacker_vcpu, attacker_disk_gb,
                      target_ram_mb, target_vcpu, target_disk_gb):
    """Bind a challenge to pinned template versions + sizing (§10)."""
    from portal.apps.content.models import Challenge
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    try:
        challenge = Challenge.objects.get(pk=challenge_id)
    except Challenge.DoesNotExist:
        raise ServiceError("Challenge not found.", 404)
    for role, version in (("attacker", attacker_version), ("target", target_version)):
        if version and not MachineImage.objects.filter(role=role, version=version).exists():
            raise ServiceError(f"No {role} machine image named '{version}'. Upload it first.", 400)
    template, _ = LabTemplate.objects.get_or_create(challenge=challenge)
    template.attacker_version = attacker_version or ""
    template.target_version = target_version or ""
    template.attacker_ram_mb = attacker_ram_mb
    template.attacker_vcpu = attacker_vcpu
    template.attacker_disk_gb = attacker_disk_gb
    template.target_ram_mb = target_ram_mb
    template.target_vcpu = target_vcpu
    template.target_disk_gb = target_disk_gb
    template.save()
    DataRevision.bump()
    return template
