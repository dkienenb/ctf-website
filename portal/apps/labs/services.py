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
import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
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
        elif command.operation.startswith("build-"):
            _apply_command_result(command)
    return {"message": "Command recorded.", "status": command.status}


def _apply_command_result(command):
    if command.operation.startswith("build-"):
        # Build commands carry no lab FK; their results fold into the
        # MachineBuild record instead (§10 interactive template building).
        _apply_build_result(command, command.status == HostCommand.Status.COMPLETED)
        return
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
    elif operation.startswith("build-"):
        _apply_build_result(command, ok)


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
STAGE_CHUNK_BYTES = 1024 * 1024  # 1 MiB — base64 inflates to ~1.33 MiB per request, safely under Django's 2.5 MB default body limit


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


def _validate_qcow2(disk_file):
    """Allocation backs every guest disk as qcow2; verify the magic bytes so
    an ISO (or anything else) never becomes a backing image."""
    head = disk_file.read(4)
    disk_file.seek(0)
    if head != b"QFI\xfb":
        raise ServiceError(
            "The base disk must be a qcow2 image (got a different format). "
            "ISOs boot from the template's cdrom device, not this file.", 400,
        )


def save_machine_image(actor, *, role, version, domain_xml, notes="", disk=None):
    """Create a machine image, or update an existing one's metadata.

    Published versions are IMMUTABLE (owner feedback): player disks use the
    template files as backing images, so replacing a disk or changing the
    domain.xml of an existing version would invalidate running labs. Changed
    contents require a new version; notes stay editable."""
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    if role not in MachineImage.Role.values:
        raise ServiceError("Role must be attacker or target.", 400)
    validate_version(version)
    validate_domain_xml(domain_xml or "", role)

    existing = MachineImage.objects.filter(role=role, version=version).first()
    if existing:
        incomplete = not existing.disk_file  # no backing disk: cannot distribute
        if disk is not None and not incomplete:
            raise ServiceError(
                "Versions are immutable: player disks back onto this template's file. "
                "Create a new version for the changed contents.", 409,
            )
        if disk is None and incomplete:
            raise ServiceError(
                "This version has no backing disk yet (host-local capture or missed "
                "upload): upload its qcow2 disk to complete the record.", 409,
            )
        if (domain_xml or "").strip() != existing.domain_xml.strip() and not incomplete:
            raise ServiceError(
                "Versions are immutable: the domain.xml of an existing version cannot "
                "change. Create a new version for the changed contents.", 409,
            )

    with transaction.atomic():
        image, created = MachineImage.objects.select_for_update().get_or_create(
            role=role, version=version,
            defaults={"domain_xml": domain_xml, "notes": notes, "uploaded_by": actor},
        )
        if not created:
            if notes:
                image.notes = notes
            image.uploaded_by = actor
            if disk is not None:
                # Completing a record without a backing disk (host-local
                # capture with a failed transfer, or an upload that missed
                # its file): first usable disk, and the domain_xml arrives
                # with it.
                _validate_qcow2(disk)
                image.domain_xml = domain_xml
                image.disk_file.save(f"{role}-{version}-disk-template", disk, save=False)
                image.disk_file.open("rb")
                hasher = hashlib.sha256()
                for block in iter(lambda: image.disk_file.read(1024 * 1024), b""):
                    hasher.update(block)
                image.disk_file.close()
                image.disk_sha256 = hasher.hexdigest()
                image.disk_size = image.disk_file.size
            image.save(update_fields=("notes", "uploaded_by", "disk_file",
                                      "disk_sha256", "disk_size", "updated_at"))
            return image, False
        if disk is not None:
            _validate_qcow2(disk)
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
    return image, True

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


def _fetch_op_id(image, file_key, host_id, sha):
    """Fetch/verify op-id that fits within HostCommand.op_id's max_length:
    role and version are hashed rather than embedded raw (long version
    names previously overflowed the command-ID field)."""
    identity = f"{image.role}:{image.version}:{sha}"
    identity_hash = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return f"fetch-{identity_hash}-{host_id}-{file_key}"


def distribute_machine_image(actor, image_id):
    """Stage a pinned image onto every workhorse (Path A v2 transfer).

    The command carries only instructions + hashes; the agent streams the
    files from the controller over a token-authenticated GET
    (template-fetch), so multi-gigabyte images never enter memory or the
    command queue. Command identity includes role, version, and per-file
    hashes, so two versions with identical content never collide and
    re-distributions are idempotent."""
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    try:
        image = MachineImage.objects.get(pk=image_id)
    except MachineImage.DoesNotExist:
        raise ServiceError("Machine image not found.", 404)
    if not image.disk_file:
        raise ServiceError("This version has no controller-side disk (upload it, or re-run capture with transfer).", 409)
    if not image.domain_xml.strip():
        raise ServiceError("This version has no domain.xml; complete the record before distributing.", 409)
    hosts = list(Host.objects.all())
    if not hosts:
        raise ServiceError("No workhorses are registered.", 409)

    disk_sha = hashlib.sha256()
    image.disk_file.open("rb")
    for block in iter(lambda: image.disk_file.read(1024 * 1024), b""):
        disk_sha.update(block)
    image.disk_file.close()
    staged_sha = disk_sha.hexdigest()
    if staged_sha != image.disk_sha256:
        image.disk_sha256 = staged_sha
        image.save(update_fields=("disk_sha256",))
    xml_sha = hashlib.sha256(image.domain_xml.encode("utf-8")).hexdigest()
    issued = 0

    def stage(host, op_id, operation, payload):
        """Idempotent issue: existing FAILED commands return to pending so
        a re-distribution retries them; COMPLETED ones are left alone."""
        command, created = HostCommand.objects.get_or_create(
            op_id=op_id, defaults={
                "host": host, "operation": operation, "payload": payload,
            },
        )
        if not created and command.status in (HostCommand.Status.FAILED, HostCommand.Status.COMPLETED):
            # Re-distribution retries FAILED commands AND re-issues COMPLETED
            # ones: the workhorse may have wiped its staging area since the
            # last successful transfer, so re-fetching is always safe.
            command.status = HostCommand.Status.PENDING
            command.result = {}
            command.save(update_fields=("status", "result", "updated_at"))
        return command

    for host in hosts:
        stage(host, _fetch_op_id(image, "domain", host.id, xml_sha),
              "template-fetch", {
                  "role": image.role, "version": image.version, "file": "domain.xml",
                  "sha256": xml_sha,
              })
        issued += 1
        stage(host, _fetch_op_id(image, "disk", host.id, staged_sha),
              "template-fetch", {
                  "role": image.role, "version": image.version, "file": "disk-template",
                  "sha256": staged_sha, "size": image.disk_size,
              })
        issued += 1
        stage(host, _fetch_op_id(image, "verify", host.id, staged_sha),
              "template-verify", {
                  "role": image.role, "version": image.version,
                  "sha256": staged_sha, "size": image.disk_size,
                  "xml_sha256": xml_sha,
              })
        issued += 1
    DataRevision.bump()
    return {"message": f"Staging {image.role}/{image.version} to {len(hosts)} workhorse(s) ({issued} transfer instructions).",
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


def lab_template_clear(actor, challenge_id):
    """Mark a challenge as having NO machine: the lab template is removed
    and the challenge page stops offering desktops entirely (questions,
    files, hints, and scores are untouched).

    Holding labs (reserved/running/saving/recovery) are refused — finish or
    recover them first. Saved/unavailable instances are discarded with their
    leases, and any queued requests for the challenge are deactivated so the
    queue cannot promote a fresh lab onto a machine-less challenge."""
    from portal.apps.content.models import Challenge
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    try:
        challenge = Challenge.objects.get(pk=challenge_id)
    except Challenge.DoesNotExist:
        raise ServiceError("Challenge not found.", 404)
    holding = LabInstance.objects.filter(challenge=challenge, state__in=LabInstance.HOLDING_STATES)
    if holding.exists():
        raise ServiceError(
            "This challenge still holds lab slots (reserved/running/saving/recovery). "
            "Have players release them or issue resets before removing the machine.", 409,
        )
    removed = LabInstance.objects.filter(challenge=challenge).count()
    LabInstance.objects.filter(challenge=challenge).delete()
    QueueEntry.objects.filter(challenge=challenge, active=True).update(active=False)
    LabTemplate.objects.filter(challenge=challenge).delete()
    from portal.apps.submissions.models import AuditEvent
    AuditEvent.objects.create(actor=actor, operation="machine-clear", reason="",
                              payload={"challenge": challenge.id, "instances_discarded": removed})
    DataRevision.bump()
    return {"instances_discarded": removed}


# --- Interactive template building (§10) --------------------------------------

def start_build(actor, *, host_id, role, version, base_version=""):
    """Boot a scratch build VM on a workhorse (one build slot per host).

    The host row is locked inside the transaction so two simultaneous
    requests cannot both pass the slot check, and the version name is
    reserved against active builds and existing images."""
    from portal.apps.submissions.models import DataRevision
    from .models import MachineBuild

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    if role not in MachineImage.Role.values:
        raise ServiceError("Role must be attacker or target.", 400)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version or ""):
        raise ServiceError("Version must start with a letter or digit and use only letters, digits, dots, dashes, underscores.", 400)
    with transaction.atomic():
        if MachineImage.objects.filter(role=role, version=version).exists():
            raise ServiceError("That version already exists; pick another name.", 409)
        active = MachineBuild.objects.filter(
            host_id=host_id,
            state__in=(MachineBuild.State.STARTING, MachineBuild.State.READY,
                       MachineBuild.State.CAPTURING, MachineBuild.State.DISCARDING,
                       MachineBuild.State.FAILED),
        )
        if active.filter(version=version).exists():
            raise ServiceError("An active build already reserves that version name.", 409)
        if active.exists():
            raise ServiceError("This workhorse already has an active build. Capture or discard it first.", 409)
        try:
            host = Host.objects.select_for_update().get(pk=host_id, online=True)
        except Host.DoesNotExist:
            raise ServiceError("Host not found or offline.", 404)
        if not host.admits:
            raise ServiceError("This workhorse is not admitting new machines.", 409)
        build = MachineBuild.objects.create(
            host=host, role=role, version=version, base_version=base_version,
            ssh_user="",
        )
        HostCommand.objects.create(
            op_id=f"build-{build.id}-start", host=host, operation="build-start",
            payload={"role": role, "version": version, "base_version": base_version},
            expected_generation=1,
        )
    DataRevision.bump()
    return build


def capture_build(actor, build_id):
    """Capture the customized build disk as a host-local template version.

    Idempotent and explicitly tracked: a repeated click returns the existing
    pending capture; the build enters CAPTURING so command batches and
    discards cannot overlap a conversion."""
    from .models import MachineBuild

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    with transaction.atomic():
        build = MachineBuild.objects.select_for_update().get(pk=build_id)
        if build.state == MachineBuild.State.CAPTURING:
            existing = HostCommand.objects.filter(
                op_id=f"build-{build.id}-capture", host=build.host).first()
            if existing and existing.status == HostCommand.Status.PENDING:
                return existing  # same capture already queued
        if build.state not in (MachineBuild.State.READY, MachineBuild.State.FAILED):
            raise ServiceError("Only an active build can be captured.", 409)
        command, created = HostCommand.objects.get_or_create(
            op_id=f"build-{build.id}-capture", host=build.host,
            defaults={
                "operation": "build-capture",
                "payload": {"role": build.role, "version": build.version,
                            "base_version": build.base_version, "build_id": build.id},
                "expected_generation": 1,
            },
        )
        if not created and command.status == HostCommand.Status.FAILED:
            command.status = HostCommand.Status.PENDING
            command.result = {}
            command.save(update_fields=("status", "result", "updated_at"))
        build.state = MachineBuild.State.CAPTURING
        build.save(update_fields=("state", "updated_at"))
    return command


def retry_build_upload(actor, build_id):
    """Re-issue the reverse upload of a capture's artifacts.

    After a successful capture the agent pushes domain.xml + disk-template
    back to the controller so the template becomes distributable. If that
    push failed (e.g. the oversized-chunk rejection), the template sits
    host-local and the controller record has no disk; this queues a
    build-upload command for the agent to push again. Idempotent: the
    controller re-assembles, verifies the sha256, and re-attaches."""
    from .models import MachineBuild
    from portal.apps.submissions.models import DataRevision

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    with transaction.atomic():
        build = MachineBuild.objects.select_for_update().select_related("host").get(pk=build_id)
        if build.state not in (MachineBuild.State.CAPTURED, MachineBuild.State.DISCARDED):
            raise ServiceError("Only a captured build can re-upload its template.", 409)
        if not build.host.online:
            raise ServiceError("This build's workhorse is offline; upload needs the template file it holds.", 409)
        command, created = HostCommand.objects.get_or_create(
            op_id=f"build-{build.id}-upload", host=build.host,
            defaults={
                "operation": "build-upload",
                "payload": {"role": build.role, "version": build.version,
                            "build_id": build.id},
                "expected_generation": 1,
            },
        )
        if not created and command.status in (HostCommand.Status.FAILED, HostCommand.Status.COMPLETED):
            command.status = HostCommand.Status.PENDING
            command.result = {}
            command.save(update_fields=("status", "result", "updated_at"))
    DataRevision.bump()
    return command


def discard_build(actor, build_id):
    """Destroy the scratch build VM without capturing (explicitly tracked)."""
    from .models import MachineBuild

    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)
    with transaction.atomic():
        build = MachineBuild.objects.select_for_update().get(pk=build_id)
        if build.state == MachineBuild.State.DISCARDING:
            existing = HostCommand.objects.filter(
                op_id=f"build-{build.id}-discard", host=build.host).first()
            if existing and existing.status == HostCommand.Status.PENDING:
                return existing
        if build.state not in (MachineBuild.State.STARTING, MachineBuild.State.READY,
                               MachineBuild.State.FAILED):
            raise ServiceError("Only an active build can be discarded.", 409)
        command, created = HostCommand.objects.get_or_create(
            op_id=f"build-{build.id}-discard", host=build.host,
            defaults={
                "operation": "build-discard", "payload": {"role": build.role},
                "expected_generation": 1,
            },
        )
        if not created and command.status == HostCommand.Status.FAILED:
            command.status = HostCommand.Status.PENDING
            command.result = {}
            command.save(update_fields=("status", "result", "updated_at"))
        build.state = MachineBuild.State.DISCARDING
        build.save(update_fields=("state", "updated_at"))
    return command


def _apply_build_result(command, ok):
    """Fold a build command's result into the MachineBuild record.

    exec accuracy: a nonzero guest script exit is recorded as FAILED output
    (the agent command itself succeeded — those are different things) and a
    failed batch blocks capture. SSH passwords are scrubbed from the stored
    payload once the result lands."""
    from .models import MachineBuild

    op_id = command.op_id
    build_id = int(op_id.split("-")[1]) if op_id.startswith("build-") else None
    if build_id is None:
        return
    try:
        build = MachineBuild.objects.select_for_update().get(pk=build_id)
    except MachineBuild.DoesNotExist:
        return
    detail = command.result or {}
    if command.operation == "build-start":
        build.state = MachineBuild.State.READY if ok else MachineBuild.State.FAILED
        build.vnc_port = detail.get("vnc_port", "") if ok else ""
    elif command.operation == "build-capture":
        if ok:
            sha, size = detail.get("sha256", ""), detail.get("size", 0)
            image, created = MachineImage.objects.get_or_create(
                role=build.role, version=build.version,
                defaults={
                    "domain_xml": "", "disk_file": None,
                    "disk_sha256": sha, "disk_size": size,
                    "notes": f"Captured on {build.host.name} from base "
                             f"{build.base_version or 'demo'} (host-local until the agent "
                             "transfers the files).",
                },
            )
            if not created and not image.disk_sha256:
                image.disk_sha256, image.disk_size = sha, size
                image.save(update_fields=("disk_sha256", "disk_size", "updated_at"))
            # Never clobber an already-transferred file with host-local data.
            build.state = MachineBuild.State.CAPTURED
        else:
            build.state = MachineBuild.State.FAILED
    elif command.operation == "build-discard":
        build.state = MachineBuild.State.DISCARDED if ok else MachineBuild.State.FAILED
    build.save()


def receive_build_artifact(build, *, file_name, seq, total, blob, sha, size, chunk_bytes=None):
    """Assemble an agent-pushed capture artifact (chunked, verified) and
    attach it to the build's MachineImage record.

    Chunks land DIRECTLY at their file offset (chunk N of a multi-chunk
    artifact is written at (N-1)·chunk_bytes) — no per-chunk part files and
    no assembled copy, so a multi-gigabyte capture needs exactly its own
    size in free space, arrival order is irrelevant, and a re-sent chunk is
    an idempotent overwrite. Completeness is tracked with zero-byte marker
    files; once every chunk is staged the file is streamed-verified against
    the sha256 and attached (renamed into place — same filesystem, so the
    attach never duplicates the data)."""
    import os as os_mod

    try:
        seq, total = int(seq), int(total)
    except (TypeError, ValueError):
        raise ServiceError("Bad chunk indexes.", 400)
    if not 1 <= seq <= total:
        raise ServiceError("Chunk seq outside 1..total.", 400)
    if total > 1:
        try:
            chunk_bytes = int(chunk_bytes)
        except (TypeError, ValueError):
            raise ServiceError("Multi-chunk artifacts require chunk_bytes.", 400)
        if chunk_bytes <= 0:
            raise ServiceError("chunk_bytes must be positive.", 400)

    staging_root = Path(settings.PRIVATE_TEMPLATES_DIR) / ".build-upload" / f"build-{build.id}"
    staging_root.mkdir(parents=True, exist_ok=True)
    target = staging_root / file_name
    marker = staging_root / f"{file_name}.received{seq:06d}"
    if total == 1:
        target.write_bytes(blob)
    else:
        with target.open("r+b" if target.exists() else "wb") as out:
            out.seek((seq - 1) * chunk_bytes)
            out.write(blob)
    marker.write_bytes(b"")  # zero-byte completion marker for this chunk

    markers = len(list(staging_root.glob(f"{file_name}.received*")))
    if markers < total:
        return {"staged": f"{markers}/{total}", "complete": False}
    if markers > total:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise ServiceError("Staging holds more chunks than the transfer declared.", 409)

    hasher = hashlib.sha256()
    with target.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            hasher.update(block)
    if hasher.hexdigest() != sha:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise ServiceError("Uploaded artifact failed sha256 verification.", 409)

    image, _ = MachineImage.objects.get_or_create(
        role=build.role, version=build.version,
        defaults={"notes": f"Captured on {build.host.name} from base {build.base_version or 'demo'}."},
    )
    if file_name == "domain.xml":
        image.domain_xml = target.read_text()
        image.save(update_fields=("domain_xml", "updated_at"))
    else:
        final_name = f"images/{build.role}-{build.version}-disk-template"
        storage = image.disk_file.storage
        try:
            # Same filesystem: rename into place instead of copying through
            # Django's storage.save (which would hold two copies of a
            # multi-gigabyte disk at peak).
            final_path = storage.path(final_name)
            Path(final_path).parent.mkdir(parents=True, exist_ok=True)
            os_mod.replace(target, final_path)
            if image.disk_file and image.disk_file.name and image.disk_file.name != final_name:
                image.disk_file.delete(save=False)
            image.disk_file.name = final_name
            image.disk_sha256 = sha
            image.disk_size = size or storage.size(final_name)
            image.save(update_fields=("disk_file", "disk_sha256", "disk_size", "updated_at"))
        except (NotImplementedError, AttributeError):
            # Non-filesystem storage: fall back to the streaming copy.
            if image.disk_file:
                image.disk_file.delete(save=False)
            image.disk_file.save(final_name.split("images/")[-1], target.open("rb"), save=False)
            image.disk_sha256 = sha
            image.disk_size = size or image.disk_file.size
            image.save(update_fields=("disk_file", "disk_sha256", "disk_size", "updated_at"))
    # Remove notes claiming host-local once the controller holds the files.
    if image.notes and "host-local" in image.notes:
        image.notes = f"Captured on {build.host.name} from base {build.base_version or 'demo'}; transferred to the controller."
        image.save(update_fields=("notes", "updated_at"))
    shutil.rmtree(staging_root, ignore_errors=True)
    return {"staged": f"{total}/{total}", "complete": True}
