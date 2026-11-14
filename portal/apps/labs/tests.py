"""Ports of the prototype lab rule tests against the production services (handoff §11)."""
import threading
from datetime import timedelta

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge
from portal.apps.labs import services
from portal.apps.labs.models import Host, LabInstance, LabTemplate, Lease, QueueEntry
from portal.apps.submissions.models import Notification
from portal.apps.submissions.services import ServiceError

from portal.apps.submissions.tests import make_user


def make_lab_challenge():
    template = LabTemplate.objects.create(attacker_version="kali-v1", target_version="target-v1")
    challenge = Challenge.objects.create(
        title=f"Lab {LabTemplate.objects.count()}",
        slug=f"lab-{template.id}-{LabTemplate.objects.count()}",
        lifecycle=Challenge.Lifecycle.PUBLISHED,
        lab_template=template,
    )
    return challenge


def make_host(name="workhorse-01"):
    host, _ = Host.objects.get_or_create(name=name)
    return host


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
        # Claim starts the lease at 30 minutes.
        services.claim(self.user, self.challenge.id)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "running")
        lease = lab.leases.order_by("-id").first()
        self.assertAlmostEqual((lease.expires_at - timezone.now()).total_seconds(), 1800, delta=3)
        # Another user queues: refresh denied, expiry unchanged.
        services.request_lab(self.other, self.other_challenge.id)
        # Slot is held by user 1, so the other user stays queued with a saved lab.
        other_lab = LabInstance.objects.get(user=self.other, challenge=self.other_challenge)
        self.assertEqual(other_lab.state, "saved")
        expiry_before = lab.leases.order_by("-id").first().expires_at
        with self.assertRaises(ServiceError) as error:
            services.refresh(self.user, self.challenge.id)
        self.assertEqual(error.exception.status, 409)
        lease = lab.leases.order_by("-id").first()
        self.assertEqual(lease.expires_at, expiry_before)
        # Release promotes the queued user with a fresh reservation.
        services.release(self.user, self.challenge.id)
        other_lab.refresh_from_db()
        self.assertEqual(other_lab.state, "reserved")

    def test_expiry_saves_and_warnings_fire_once(self):
        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        lease = lab.leases.order_by("-id").first()
        lease.expires_at = timezone.now() + timedelta(seconds=50)
        lease.save()
        services.tick()
        messages = list(Notification.objects.filter(recipient=self.user).values_list("message", flat=True))
        self.assertTrue(any("5 minute" in m for m in messages))
        self.assertTrue(any("1 minute" in m for m in messages))
        services.tick()  # deduplicated
        self.assertEqual(Notification.objects.filter(recipient=self.user, message__icontains="5 minute").count(), 1)
        # Expiry saves the lab.
        lease.refresh_from_db()
        lease.expires_at = timezone.now() - timedelta(seconds=1)
        lease.save()
        services.tick()
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        self.assertFalse(lab.leases.filter(active=True).exists())

    def test_claim_timeout_passes_slot_without_destroying_state(self):
        services.request_lab(self.user, self.challenge.id)
        services.request_lab(self.other, self.other_challenge.id)  # queued behind
        entry = QueueEntry.objects.filter(user=self.other).order_by("-id").first()
        self.assertTrue(entry.active)  # still waiting
        # User 1's reservation lapses.
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        claimed = QueueEntry.objects.filter(user=self.user, challenge=self.challenge, active=False).order_by("-id").first()
        claimed.claimed_at = timezone.now() - timedelta(seconds=121)
        claimed.save()
        services.tick()
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")  # retained, not reset
        other_lab = LabInstance.objects.get(user=self.other, challenge=self.other_challenge)
        self.assertEqual(other_lab.state, "reserved")  # slot passed onward

    def test_host_outage_preserves_state(self):
        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)
        services.set_host_online(self.admin_user, self.host.id, False)
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(lab.state, "unavailable")
        with self.assertRaises(ServiceError) as error:
            services.request_lab(self.other, self.other_challenge.id)
        self.assertEqual(error.exception.status, 503)
        services.set_host_online(self.admin_user, self.host.id, True)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        services.request_lab(self.user, self.challenge.id)  # re-request works
        lab.refresh_from_db()
        self.assertEqual(lab.state, "reserved")

    def test_reset_request_notifies_admins_only(self):
        admin = self.admin_user
        services.request_lab(self.user, self.challenge.id)
        result = services.request_admin_reset(self.user, self.challenge.id)
        self.assertIn("Administrator reset requested", result["message"])
        self.assertTrue(Notification.objects.filter(recipient=admin, message__icontains="requested an admin reset").exists())
        lab = LabInstance.objects.get(user=self.user, challenge=self.challenge)
        self.assertEqual(lab.state, "reserved")  # nothing self-reset
        self.assertFalse(Notification.objects.filter(recipient=self.user, message__icontains="restored to its").exists())

    def test_visibility_and_approval_gates(self):
        pending = make_user("600000000000000004", "Pending", status=User.Status.PENDING)
        with self.assertRaises(ServiceError) as error:
            services.request_lab(pending, self.challenge.id)
        self.assertEqual(error.exception.status, 403)
        self.challenge.lifecycle = Challenge.Lifecycle.DRAFT
        self.challenge.save()
        with self.assertRaises(ServiceError):
            services.request_lab(self.user, self.challenge.id)


class LabConcurrencyTests(TransactionTestCase):
    @override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
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
        # Other requests either wait in the queue or are rejected — never a
        # second allocation.
        self.assertLessEqual(len(successes) + len(errors), 3)
