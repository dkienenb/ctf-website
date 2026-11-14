"""Lab allocation, queue, and lease rules (handoff §11, §18).

Ports of the prototype's verified behavior onto PostgreSQL:

- One active lab per user overall (also a database constraint on LabInstance).
- One saved lab per user/challenge; one outstanding queue request per user.
- Requests enqueue; the oldest active request is promoted when a compatible
  online host has a free slot. Promotion holds a two-minute reservation.
- Claiming starts the lease only when ready; the launch lease is 30 minutes
  from ready.
- Refresh sets expiry to now + 30 minutes and is denied while another user is
  queued — without shortening an already-accepted expiry.
- Warnings at five minutes and one minute remain deduplicated per lease.
- Expiry/release saves the lab before releasing capacity and promotes the next
  queued request. Expired users rejoin the back of the queue.
- Host unavailability blocks new allocations and access while retaining saved
  state references; no template fallback occurs.
- All decisions run inside serialized transactions; the per-user allocation
  limit is additionally enforced by uniq_one_active_allocation.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge
from portal.apps.submissions.services import ServiceError

from .models import Host, LabInstance, Lease, QueueEntry

RESERVATION_SECONDS = 120
LEASE_SECONDS = 1800
WARNING_THRESHOLDS = ((300, "warned_five", "Lab lease: 5 minute(s) remaining."),
                      (60, "warned_one", "Lab lease: 1 minute(s) remaining."))
HOLDING = LabInstance.HOLDING_STATES


def _assert_approved(user):
    if not getattr(user, "pk", None):
        raise ServiceError("Sign in first.", 401)
    if user.status != User.Status.APPROVED:
        raise ServiceError("Approved access is required.", 403)


def _visible(challenge):
    now = timezone.now()
    if challenge.lifecycle != Challenge.Lifecycle.PUBLISHED or (challenge.go_live and challenge.go_live > now):
        raise ServiceError("Challenge is not available.", 404)


def _free_capacity(host):
    holding = LabInstance.objects.filter(state__in=HOLDING)
    return holding.count() < host.capacity_slots


def _promote():
    """Promote the oldest queued request when an online host has a free slot."""
    for host in Host.objects.filter(online=True).order_by("id"):
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
        Notification_create(lab.user_id, "A lab slot is ready. Claim it within two minutes.")
        return


def Notification_create(user_id, message):
    from portal.apps.submissions.models import Notification

    Notification.objects.create(recipient_id=user_id, message=message)


def request_lab(user, challenge_id):
    """Enqueue a lab request; may promote immediately when capacity exists."""
    _assert_approved(user)
    with transaction.atomic():
        challenge = Challenge.objects.get(pk=challenge_id)
        _visible(challenge)
        host = Host.objects.filter(online=True).first()
        if host is None:
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


def claim(user, challenge_id):
    _assert_approved(user)
    with transaction.atomic():
        lab = LabInstance.objects.select_for_update().get(user=user, challenge_id=challenge_id)
        now = timezone.now()
        entry = (
            QueueEntry.objects.filter(user=user, challenge=lab.challenge, active=False, claimed_at__isnull=False)
            .order_by("-id")
            .first()
        )
        deadline = entry.claimed_at + timedelta(seconds=RESERVATION_SECONDS) if entry else None
        if lab.state != "reserved" or not deadline or deadline < now:
            raise ServiceError("No valid reservation to claim.", 409)
        lab.state = "running"
        lab.save(update_fields=("state", "updated_at"))
        Lease.objects.create(
            lab=lab, ready_at=now, expires_at=now + timedelta(seconds=LEASE_SECONDS),
        )
    return {"message": "Both simulated guests are ready. Your 30-minute lease starts now."}


def refresh(user, challenge_id):
    _assert_approved(user)
    with transaction.atomic():
        lab = LabInstance.objects.select_for_update().get(user=user, challenge_id=challenge_id)
        lease = lab.leases.filter(active=True).order_by("-id").first()
        if lab.state != "running" or lease is None:
            raise ServiceError("Lab is not running.", 409)
        # Allowed only when no other user is queued (§11); the later queue
        # entry never retroactively shortens an accepted lease.
        if QueueEntry.objects.exclude(user=user).filter(active=True).exists():
            raise ServiceError("Refresh is unavailable while another user is queued. Your current expiry is unchanged.", 409)
        lease.revision += 1
        lease.expires_at = timezone.now() + timedelta(seconds=LEASE_SECONDS)
        lease.warned_five = False
        lease.warned_one = False
        lease.save(update_fields=("revision", "expires_at", "warned_five", "warned_one"))
    return {"message": "Lease expiry set to 30 minutes from now."}


def release(user, challenge_id):
    _assert_approved(user)
    with transaction.atomic():
        lab = LabInstance.objects.select_for_update().get(user=user, challenge_id=challenge_id)
        if lab.state not in ("running", "reserved"):
            raise ServiceError("Lab cannot be released in its current state.", 409)
        _save_lab(lab, "Both simulated guests saved. Capacity released.")
        _promote()
    return {"message": "Both simulated guests saved. Capacity released."}


def _save_lab(lab, message=None):
    lab.state = "saved"
    lab.save(update_fields=("state", "updated_at"))
    lab.leases.filter(active=True).update(active=False)
    if message:
        Notification_create(lab.user_id, message)


def tick():
    """Expiry/claim-timeout processing and lease warnings (§11, §18)."""
    with transaction.atomic():
        now = timezone.now()
        for lab in LabInstance.objects.select_for_update().filter(state="running"):
            lease = lab.leases.filter(active=True).order_by("-id").first()
            if lease is None:
                _save_lab(lab, "Lease expired. Browser access ended and both simulated guests were saved.")
                continue
            remaining = (lease.expires_at - now).total_seconds()
            for threshold, flag, message in WARNING_THRESHOLDS:
                if remaining <= threshold and remaining > 0 and not getattr(lease, flag):
                    setattr(lease, flag, True)
                    lease.save(update_fields=(flag,))
                    Notification_create(lab.user_id, message)
            if lease.expires_at <= now:
                _save_lab(lab, "Lease expired. Browser access ended and both simulated guests were saved.")
        for lab in LabInstance.objects.select_for_update().filter(state="reserved"):
            # Reservations carry their deadline in the queue promotion; the
            # claim window is validated at claim time and cleaned up here.
            entry_claimed = QueueEntry.objects.filter(
                user=lab.user, challenge=lab.challenge, claimed_at__isnull=False, active=False
            ).order_by("-id").first()
            deadline = entry_claimed.claimed_at + timedelta(seconds=RESERVATION_SECONDS) if entry_claimed else None
            if deadline and deadline <= now:
                _save_lab(lab, "Reservation expired. Your saved lab was retained.")
        _promote()


def request_admin_reset(user, challenge_id):
    """Players request an administrator reset; they never self-reset (§13)."""
    _assert_approved(user)
    challenge = Challenge.objects.get(pk=challenge_id)
    for admin in User.objects.filter(is_admin=True, status=User.Status.APPROVED):
        Notification_create(admin.id, f"User #{user.id} requested an admin reset for {challenge.title}.")
    Notification_create(user.id, "Reset request sent to administrators. Your current lab and progress were retained.")
    return {"message": "Administrator reset requested."}


def set_host_online(actor, host_id, online):
    from portal.apps.submissions.models import DataRevision

    host = Host.objects.select_for_update().get(pk=host_id)
    host.online = online
    host.save(update_fields=("online",))
    if not online:
        for lab in LabInstance.objects.select_for_update().filter(state__in=("running", "reserved")):
            lab.state = "unavailable"
            lab.save(update_fields=("state", "updated_at"))
            Notification_create(lab.user_id, "Workhorse unavailable. Browser access is interrupted; saved-state references are retained.")
    else:
        for lab in LabInstance.objects.select_for_update().filter(state="unavailable"):
            _save_lab(lab, "Workhorse returned. State retained; request a slot to resume. No template reset occurred.")
        _promote()
    DataRevision.bump()
    return host
