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
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge
from portal.apps.submissions.services import ServiceError

from .models import Host, HostCommand, LabInstance, Lease, QueueEntry

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
    """Promote the oldest queued request when a host has a free slot."""
    for host in _admitting_hosts():
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
        challenge = Challenge.objects.get(pk=challenge_id)
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
        lab = LabInstance.objects.select_for_update().get(user=user, challenge_id=challenge_id)
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
        lab = LabInstance.objects.select_for_update().get(user=user, challenge_id=challenge_id)
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
        lab = LabInstance.objects.select_for_update().get(user=user, challenge_id=challenge_id)
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
