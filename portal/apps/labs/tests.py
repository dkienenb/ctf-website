"""Ports of the prototype lab rule tests against the production services
(handoff §11, §13, §18) including the durable host-command flow."""
import threading
from datetime import timedelta

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge
from portal.apps.labs import services
from portal.apps.labs.models import Host, HostCommand, LabInstance, LabTemplate, Lease, QueueEntry
from portal.apps.submissions.models import Notification
from portal.apps.submissions.services import ServiceError

from portal.apps.submissions.tests import make_user


def make_lab_challenge():
    template = LabTemplate.objects.create(attacker_version="kali-v1", target_version="target-v1")
    challenge = Challenge.objects.create(
        title=f"Lab {template.id}",
        slug=f"lab-{template.id}",
        lifecycle=Challenge.Lifecycle.PUBLISHED,
        lab_template=template,
    )
    return challenge


def make_host(name="workhorse-01"):
    host, _ = Host.objects.get_or_create(name=name)
    host.online = True
    host.total_disk_gb = 100
    host.free_disk_gb = 80
    host.total_ram_gb = 32
    host.free_ram_gb = 26
    host.last_heartbeat = timezone.now()
    host.save()
    return host


def complete(host, operation_hint=None, status="ok"):
    """Complete the oldest pending command for the host (agent simulation)."""
    pending = HostCommand.objects.filter(host=host, status=HostCommand.Status.PENDING).order_by("id").first()
    assert pending is not None, f"no pending command (wanted {operation_hint})"
    if operation_hint:
        assert pending.operation == operation_hint, f"expected {operation_hint}, got {pending.operation}"
    services.complete_command(host, pending.op_id, status, {})
    return pending


class LabRuleTests(TestCase):
    def setUp(self):
        self.host = make_host()
        self.user = make_user("600000000000000001", "Alex")
        self.other = make_user("600000000000000002", "Sam")
        self.admin_user = make_user("600000000000000003", "Admin", is_admin=True)
        self.challenge = make_lab_challenge()
        self.other_challenge = make_lab_challenge()

    def test_request_claim_queue_and_refresh_rules(self):
        result = services.request_lab(self.user, self.challenge.id)
        self.assertIn("reserved", result["message"])
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(lab.state, "reserved")
        # One outstanding request per user.
        with self.assertRaises(ServiceError) as error:
            services.request_lab(self.user, self.other_challenge.id)
        self.assertEqual(error.exception.status, 409)
        # Claim issues the durable allocate command; lease waits for readiness.
        result = services.claim(self.user, self.challenge.id)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "starting")
        command = HostCommand.objects.filter(lab=lab, operation="allocate").first()
        self.assertIsNotNone(command)
        self.assertEqual(command.expected_generation, lab.generation)
        # Another user queues (slot still held by the starting lab).
        services.request_lab(self.other, self.other_challenge.id)
        other_lab = LabInstance.objects.get(user=self.other, challenge=self.other_challenge)
        self.assertEqual(other_lab.state, "saved")
        # Agent reports ready: lease starts now (§11), 30 minutes.
        services.complete_command(self.host, command.op_id, "ok", {"domains": ["x", "y"]})
        lab.refresh_from_db()
        self.assertEqual(lab.state, "running")
        lease = lab.leases.order_by("-id").first()
        self.assertAlmostEqual((lease.expires_at - timezone.now()).total_seconds(), 1800, delta=3)
        expiry_before = lease.expires_at
        # Refresh denied while another user is queued; expiry unchanged.
        with self.assertRaises(ServiceError) as error:
            services.refresh(self.user, self.challenge.id)
        self.assertEqual(error.exception.status, 409)
        lease.refresh_from_db()
        self.assertEqual(lease.expires_at, expiry_before)
        # Release issues save; capacity frees only after the save completes.
        services.release(self.user, self.challenge.id)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saving")
        other_lab.refresh_from_db()
        self.assertEqual(other_lab.state, "saved")  # still queued
        complete(self.host, "save")
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        other_lab.refresh_from_db()
        self.assertEqual(other_lab.state, "reserved")  # promoted

    def test_expiry_saves_and_warnings_fire_once(self):
        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)
        complete(self.host, "allocate")
        lease = Lease.objects.order_by("-id").first()
        lease.expires_at = timezone.now() + timedelta(seconds=50)
        lease.save()
        services.tick()
        messages = list(Notification.objects.filter(recipient=self.user).values_list("message", flat=True))
        self.assertTrue(any("5 minute" in m for m in messages))
        self.assertTrue(any("1 minute" in m for m in messages))
        services.tick()  # warnings deduplicated per lease
        self.assertEqual(Notification.objects.filter(recipient=self.user, message__icontains="5 minute").count(), 1)
        # Expiry transitions to saving with a durable save command.
        lease.refresh_from_db()
        lease.expires_at = timezone.now() - timedelta(seconds=1)
        lease.save()
        services.tick()
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(lab.state, "saving")
        complete(self.host, "save")
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        self.assertFalse(lab.leases.filter(active=True).exists())

    def test_claim_timeout_passes_slot_without_destroying_state(self):
        services.request_lab(self.user, self.challenge.id)
        services.request_lab(self.other, self.other_challenge.id)
        entry = QueueEntry.objects.filter(user=self.other).order_by("-id").first()
        self.assertTrue(entry.active)
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        claimed = QueueEntry.objects.filter(user=self.user, challenge=self.challenge, active=False).order_by("-id").first()
        claimed.claimed_at = timezone.now() - timedelta(seconds=121)
        claimed.save()
        services.tick()
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")  # retained, not reset
        other_lab = LabInstance.objects.get(user=self.other, challenge=self.other_challenge)
        self.assertEqual(other_lab.state, "reserved")

    def test_failed_allocate_returns_to_saved(self):
        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)
        complete(self.host, "allocate", status="failed")
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(lab.state, "saved")
        self.assertTrue(Notification.objects.filter(recipient=self.user, message__icontains="could not start").exists())

    def test_save_failure_keeps_reservation_and_alerts_admins(self):
        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)
        complete(self.host, "allocate")
        services.release(self.user, self.challenge.id)
        complete(self.host, "save", status="failed")
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(lab.state, "recovery")  # reservation retained (§18)
        self.assertTrue(Notification.objects.filter(recipient=self.admin_user, message__icontains="Recovery required").exists())
        # Admin recovery issues the durable recover command.
        services.admin_reset(self.admin_user, self.user.id, self.challenge.id, "recover")
        complete(self.host, "recover")
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        self.assertTrue(Notification.objects.filter(recipient=self.user, message__icontains="restored to its pinned").exists())

    def test_host_outage_preserves_state(self):
        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)
        complete(self.host, "allocate")
        services.set_host_online(self.admin_user, self.host.id, False)
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(lab.state, "unavailable")
        with self.assertRaises(ServiceError) as error:
            services.request_lab(self.other, self.other_challenge.id)
        self.assertEqual(error.exception.status, 503)
        services.set_host_online(self.admin_user, self.host.id, True)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        services.request_lab(self.user, self.challenge.id)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "reserved")

    def test_admin_resets_are_durable_and_generation_checked(self):
        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)
        complete(self.host, "allocate")
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        generation_before = lab.generation
        services.admin_reset(self.admin_user, self.user.id, self.challenge.id, "target")
        command = complete(self.host, "reset-target")
        lab.refresh_from_db()
        self.assertEqual(lab.target_generation, 2)
        self.assertEqual(lab.generation, generation_before)  # attacker untouched
        self.assertTrue(Notification.objects.filter(recipient=self.user, message__icontains="Target reset").exists())
        # Full reset advances the generation; stale commands are rejected.
        services.admin_reset(self.admin_user, self.user.id, self.challenge.id, "full")
        stale_command = HostCommand.objects.filter(operation="reset-full").order_by("-id").first()
        lab.generation += 1
        lab.save()
        with self.assertRaises(ServiceError) as error:
            services.complete_command(self.host, stale_command.op_id, "ok", {})
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(
            HostCommand.objects.get(pk=stale_command.pk).status, HostCommand.Status.FAILED
        )  # the rejection persists; the agent will not re-poll it
        # A fresh reset command carries the new expected generation.
        services.admin_reset(self.admin_user, self.user.id, self.challenge.id, "full")
        complete(self.host, "reset-full")
        lab.refresh_from_db()
        self.assertEqual(lab.generation, generation_before + 2)

    def test_visibility_and_approval_gates(self):
        pending = make_user("600000000000000004", "Pending", status=User.Status.PENDING)
        with self.assertRaises(ServiceError) as error:
            services.request_lab(pending, self.challenge.id)
        self.assertEqual(error.exception.status, 403)
        self.challenge.lifecycle = Challenge.Lifecycle.DRAFT
        self.challenge.save()
        with self.assertRaises(ServiceError):
            services.request_lab(self.user, self.challenge.id)

    def test_reset_request_notifies_admins_only(self):
        services.request_lab(self.user, self.challenge.id)
        result = services.request_admin_reset(self.user, self.challenge.id)
        self.assertIn("Administrator reset requested", result["message"])
        self.assertTrue(Notification.objects.filter(recipient=self.admin_user, message__icontains="requested an admin reset").exists())
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(lab.state, "reserved")  # nothing self-reset


class LabConcurrencyTests(TransactionTestCase):
    def test_concurrent_requests_hold_one_allocation(self):
        host = make_host("workhorse-conc")
        user = make_user("600000000000000005", "Racer")
        challenges = [make_lab_challenge() for _ in range(3)]
        successes, errors = [], []

        def attempt(index):
            try:
                successes.append(services.request_lab(user, challenges[index].id))
            except ServiceError as error:
                errors.append(error.message)
            finally:
                from django.db import connections

                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        holding = LabInstance.objects.filter(user=user, state__in=LabInstance.HOLDING_STATES)
        self.assertEqual(holding.count(), 1, f"allocation rule violated: {successes} {errors}")
        self.assertLessEqual(len(successes) + len(errors), 3)
