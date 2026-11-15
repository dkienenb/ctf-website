"""End-to-end lab workflows and their negatives (handoff §11, §13, §18):
request → reserve/queue → claim → allocate → running → refresh → release →
save → restore, admission/reserve policy, outage handling, expiry, and
operator resets — including every refusal path."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from portal.apps.accounts.models import User
from portal.apps.labs import services
from portal.apps.labs.models import Host, HostCommand, LabInstance, Lease, QueueEntry
from portal.apps.submissions.models import Notification
from portal.apps.submissions.services import ServiceError
from portal.apps.testutils import complete, make_host, make_lab_challenge, make_user


class LabConcurrencyTests(TransactionTestCase):
    """Cross-connection races need committed data: TransactionTestCase."""

    def setUp(self):
        self.host = make_host()  # capacity 1: exactly one slot to fight over
        self.user_a = make_user("420000000000000001", "RacerA")
        self.user_b = make_user("420000000000000002", "RacerB")
        self.challenge = make_lab_challenge()

    def test_last_slot_never_double_booked_under_concurrency(self):
        """Two users race for one free slot. Invariants: exactly one
        reservation; the loser queues cleanly (or is refused cleanly); no
        duplicate lab rows; no raw exception escapes to a player."""
        import threading
        from django.db import connection

        results = {}

        def race(user, key):
            connection.close()  # fresh connection per thread
            try:
                results[key] = services.request_lab(user, self.challenge.id)["message"]
            except ServiceError as error:
                results[key] = f"refused: {error.message}"
            finally:
                connection.close()  # release the test DB session for teardown

        threads = [
            threading.Thread(target=race, args=(self.user_a, "a")),
            threading.Thread(target=race, args=(self.user_b, "b")),
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]

        # Both threads completed with a clean outcome (no raw crashes).
        self.assertEqual(len(results), 2, results)
        reserved = [
            key for key, message in results.items() if "reserved" in message
        ]
        self.assertEqual(len(reserved), 1, results)  # exactly one winner
        loser = "b" if reserved == ["a"] else "a"
        loser_user = self.user_a if loser == "a" else self.user_b
        self.assertIn(loser, results)
        self.assertTrue(
            "Queued" in results[loser] or "refused" in results[loser], results,
        )
        # No duplicate lab rows for anyone.
        self.assertEqual(LabInstance.objects.filter(user=loser_user).count(), 1)
        self.assertEqual(LabInstance.objects.filter(user=self.user_a).count() <= 1, True)
        self.assertEqual(LabInstance.objects.filter(user=self.user_b).count() <= 1, True)


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class LabLifecycleTests(TestCase):
    def setUp(self):
        self.host = make_host()
        self.user = make_user("400000000000000001", "Alex")
        self.other = make_user("400000000000000002", "Sam")
        self.third = make_user("400000000000000003", "Jo")
        self.challenge = make_lab_challenge()
        self.other_challenge = make_lab_challenge()

    def _lab(self, user, challenge=None):
        return LabInstance.objects.get(user=user, challenge=challenge or self.challenge)

    def _request_and_claim(self, user, challenge=None):
        services.request_lab(user, (challenge or self.challenge).id)
        services.claim(user, (challenge or self.challenge).id)
        return self._lab(user, challenge)

    # --- positive end-to-end ---------------------------------------------

    def test_full_cycle_request_claim_run_refresh_release_save_restore(self):
        # Capacity of 1 → immediate reservation, not queueing.
        result = services.request_lab(self.user, self.challenge.id)
        self.assertIn("reserved", result["message"])
        lab = self._lab(self.user)
        self.assertEqual(lab.state, "reserved")

        # Claim starts the lease window only when the agent reports ready.
        services.claim(self.user, self.challenge.id)
        self.assertEqual(self._lab(self.user).state, "starting")
        command = complete(self.host, "allocate")
        lab.refresh_from_db()
        self.assertEqual(lab.state, "running")
        lease = lab.leases.filter(active=True).get()
        self.assertAlmostEqual(
            (lease.expires_at - timezone.now()).total_seconds(), 1800, delta=3,
        )

        # Refresh re-issues the deadline and extends by the full lease.
        revision_before = lease.revision
        services.refresh(self.user, self.challenge.id)
        complete(self.host)  # the agent acknowledges the new deadline (§18)
        lease.refresh_from_db()
        self.assertEqual(lease.revision, revision_before + 1)
        deadline = HostCommand.objects.filter(
            operation="refresh-lease", lab=lab).order_by("-id").first()
        self.assertIsNotNone(deadline)
        self.assertEqual(deadline.payload["expires_at"], lease.expires_at.isoformat())

        # Release saves; capacity frees only after the agent confirms the save.
        services.release(self.user, self.challenge.id)
        self.assertEqual(self._lab(self.user).state, "saving")
        complete(self.host, "save")
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        self.assertFalse(lab.leases.filter(active=True).exists())

        # The original user can restore their workspace later (capacity free).
        services.request_lab(self.user, self.challenge.id)
        lab = self._lab(self.user)
        self.assertEqual(lab.state, "reserved")
        services.claim(self.user, self.challenge.id)
        detail = complete(self.host, "allocate")
        lab.refresh_from_db()
        self.assertEqual(lab.state, "running")

        # A queued user waits while the slot is held…
        result = services.request_lab(self.other, self.challenge.id)
        self.assertIn("Queued", result["message"])
        self.assertEqual(self._lab(self.other).state, "saved")  # queued
        # …and is promoted the moment the slot frees again.
        services.release(self.user, self.challenge.id)
        complete(self.host, "save")
        self.assertEqual(self._lab(self.other).state, "reserved")

    def test_request_beyond_capacity_queues(self):
        self._request_and_claim(self.user)
        complete(self.host, "allocate")
        result = services.request_lab(self.other, self.challenge.id)
        self.assertIn("Queued", result["message"])
        entry = QueueEntry.objects.get(user=self.other, challenge=self.challenge, active=True)
        self.assertIsNone(entry.claimed_at)  # not reserved, just queued

    def test_users_cannot_hold_two_labs_at_once(self):
        """One allocation per user across ALL challenges (holding check)."""
        self._request_and_claim(self.user, self.challenge)
        with self.assertRaises(ServiceError) as error:
            services.request_lab(self.user, self.other_challenge.id)
        self.assertEqual(error.exception.status, 409)
        self.assertIn("already hold", error.exception.message)

    # --- request negatives -------------------------------------------------

    def test_request_negatives(self):
        locked = make_user("410000000000000001", "Locked", status=User.Status.PENDING)
        with self.assertRaises(ServiceError):
            services.request_lab(locked, self.challenge.id)  # unapproved
        with self.assertRaises(ServiceError):
            services.request_lab(self.user, 999999)          # unknown challenge
        self._request_and_claim(self.user)
        complete(self.host, "allocate")
        with self.assertRaises(ServiceError) as error:
            services.request_lab(self.user, self.challenge.id)  # already holding
        self.assertEqual(error.exception.status, 409)

    def test_request_when_no_host_admits_is_503(self):
        self.host.free_disk_gb = 10  # below the 20% reserve
        self.host.save()
        with self.assertRaises(ServiceError) as error:
            services.request_lab(self.user, self.challenge.id)
        self.assertEqual(error.exception.status, 503)

    # --- claim negatives -------------------------------------------------

    def test_claim_without_reservation_is_refused(self):
        with self.assertRaises(ServiceError):
            services.claim(self.user, self.challenge.id)  # never requested

    def test_claim_twice_is_refused(self):
        self._request_and_claim(self.user)
        with self.assertRaises(ServiceError) as error:
            services.claim(self.user, self.challenge.id)
        self.assertEqual(error.exception.status, 409)

    def test_expired_reservation_cannot_be_claimed_twice(self):
        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)  # claim deactivates the entry, lab is 'starting'
        self.assertEqual(self._lab(self.user).state, "starting")
        entry = QueueEntry.objects.get(user=self.user, challenge=self.challenge, active=False, claimed_at__isnull=False)
        entry.claimed_at = timezone.now() - timedelta(seconds=3600)
        entry.save()
        with self.assertRaises(ServiceError) as error:
            services.claim(self.user, self.challenge.id)
        self.assertEqual(error.exception.status, 409)

    def test_claim_blocked_when_host_stops_admitting(self):
        services.request_lab(self.user, self.challenge.id)
        self.host.free_disk_gb = 10  # 10% free < 20% reserve
        self.host.save()
        with self.assertRaises(ServiceError) as error:
            services.claim(self.user, self.challenge.id)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(self._lab(self.user).state, "reserved")  # reservation retained

    # --- refresh / release negatives -------------------------------------

    def test_refresh_negatives(self):
        with self.assertRaises(ServiceError):
            services.refresh(self.user, self.challenge.id)  # nothing running
        self._request_and_claim(self.user)
        with self.assertRaises(ServiceError):
            services.refresh(self.user, self.challenge.id)  # still starting
        complete(self.host, "allocate")
        services.request_lab(self.other, self.challenge.id)  # someone queues
        with self.assertRaises(ServiceError):
            services.refresh(self.user, self.challenge.id)   # queue means no refresh

    def test_release_negatives(self):
        with self.assertRaises(ServiceError):
            services.release(self.user, self.challenge.id)  # nothing running
        self._request_and_claim(self.user)
        with self.assertRaises(ServiceError):
            services.release(self.user, self.challenge.id)  # not running yet

    def test_refresh_blocked_while_another_user_queued(self):
        self._request_and_claim(self.user)
        complete(self.host, "allocate")
        services.request_lab(self.other, self.challenge.id)
        with self.assertRaises(ServiceError):
            services.refresh(self.user, self.challenge.id)
        # Releasing unblocks the queue; the other user stays queued until
        # capacity is actually free (save confirmed) and a tick promotes.
        services.release(self.user, self.challenge.id)
        complete(self.host, "save")
        # Confirming the save frees capacity and the queue promotes at once.
        self.assertEqual(self._lab(self.other).state, "reserved")

    # --- expiry ------------------------------------------------------------

    def test_lease_expiry_saves_and_notifies(self):
        self._request_and_claim(self.user)
        lab = self._lab(self.user)
        complete(self.host, "allocate")
        lease = lab.leases.filter(active=True).get()
        lease.expires_at = timezone.now() - timedelta(seconds=1)
        lease.save()
        services.tick()
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saving")
        complete(self.host, "save")
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        self.assertTrue(Notification.objects.filter(recipient=self.user).exists())

    # --- outages -----------------------------------------------------------

    def test_host_outage_marks_labs_unavailable_then_recovers_to_saved(self):
        self._request_and_claim(self.user)
        lab = self._lab(self.user)
        complete(self.host, "allocate")
        services.set_host_online(self.admin_user(), self.host.id, online=False)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "unavailable")
        services.set_host_online(self.admin_user(), self.host.id, online=True)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")  # safe fallback, no template reset
        self.assertTrue(Notification.objects.filter(recipient=self.user).exists())

    def admin_user(self):
        if not hasattr(self, "_admin"):
            self._admin = make_user("419999999999999999", "Ops", is_admin=True)
        return self._admin

    def test_admission_stops_on_stale_heartbeat(self):
        self.host.last_heartbeat = timezone.now() - timedelta(seconds=300)
        self.host.save()
        with self.assertRaises(ServiceError) as error:
            services.request_lab(self.user, self.challenge.id)
        self.assertEqual(error.exception.status, 503)  # never assumes the host is gone
